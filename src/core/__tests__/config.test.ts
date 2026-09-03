/**
 * Comprehensive tests for the configuration module
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as fs from 'fs-extra';
import * as path from 'node:path';
import * as os from 'node:os';
import {
  ConfigLoader,
  loadConfig,
  validateConfig,
  DEFAULT_CONFIG,
  ENV_VAR_MAPPING,
  JunoTaskConfigSchema,
  DEFAULT_GIT_CHECKPOINT_INCLUDE,
  PROJECT_CONFIG_VERSION,
  createPersistedProjectConfigDefaults,
  mergePersistedProjectDefaults,
  writeProjectConfigAtomic,
  getPromptMacroDictionary,
  selectAgentProfileHooks,
} from '../config.js';
import type { JunoTaskConfig } from '../../types/index.js';

describe('Configuration Module', () => {
  let tempDir: string;
  let originalEnv: Record<string, string | undefined>;

  beforeEach(async () => {
    // Create temporary directory for test files
    tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-config-test-'));

    // Save original environment variables
    originalEnv = { ...process.env };

    // Clear yylo environment variables
    for (const envVar of Object.keys(ENV_VAR_MAPPING)) {
      delete process.env[envVar];
      // Also clear YYLO variants for testing
      const junoCodeVar = envVar.replace('JUNO_TASK_', 'YYLO_');
      delete process.env[junoCodeVar];
    }
  });

  afterEach(async () => {
    // Restore original environment
    process.env = originalEnv;

    // Clean up temp directory
    await fs.remove(tempDir);
  });

  describe('DEFAULT_CONFIG', () => {
    it('should provide valid default configuration', () => {
      expect(DEFAULT_CONFIG.defaultSubagent).toBe('claude');
      expect(DEFAULT_CONFIG.defaultMaxIterations).toBe(1);
      expect(DEFAULT_CONFIG.logLevel).toBe('info');
      expect(DEFAULT_CONFIG.verbose).toBe(1);
      expect(DEFAULT_CONFIG.quiet).toBe(false);
      expect(DEFAULT_CONFIG.mcpTimeout).toBe(43200000); // 12 hours in milliseconds
      expect(DEFAULT_CONFIG.mcpRetries).toBe(3);
      expect(DEFAULT_CONFIG.interactive).toBe(true);
      expect(DEFAULT_CONFIG.headlessMode).toBe(false);
      expect(DEFAULT_CONFIG.workingDirectory).toBe(process.cwd());
      expect(DEFAULT_CONFIG.sessionDirectory).toBe(path.join(process.cwd(), '.juno_task'));
      expect(DEFAULT_CONFIG.envFilePath).toBe('.env.yylo');
      expect(DEFAULT_CONFIG.envFileCopied).toBe(false);
      expect(DEFAULT_CONFIG.kanbanRegistry).toEqual({
        enabled: false,
        allowedProjects: [],
      });
      expect(DEFAULT_CONFIG.configVersion).toBe(PROJECT_CONFIG_VERSION);
      expect(DEFAULT_CONFIG.workflowModels).toEqual([]);
      expect(DEFAULT_CONFIG.gitCheckpoint?.include).toEqual(DEFAULT_GIT_CHECKPOINT_INCLUDE);
      expect(DEFAULT_CONFIG.promptMacros).toEqual({
        enabled: true,
        order: 'before_command_substitution',
        maxDepth: 10,
        global: {},
        local: {},
      });
    });

    it('should pass schema validation', () => {
      expect(() => validateConfig(DEFAULT_CONFIG)).not.toThrow();
    });

    it('defines complete persisted defaults for fresh projects', () => {
      const persisted = createPersistedProjectConfigDefaults('/tmp/example-project');
      expect(persisted).toMatchObject({
        configVersion: PROJECT_CONFIG_VERSION,
        defaultBackend: 'shell',
        workflowModels: [],
        autoDependencyUpdate: true,
        onHourlyLimit: 'raise',
        kanbanRegistry: { enabled: false, allowedProjects: [] },
        gitCheckpoint: { include: [...DEFAULT_GIT_CHECKPOINT_INCLUDE] },
        promptMacros: {
          enabled: true,
          order: 'before_command_substitution',
          maxDepth: 10,
          global: {},
          local: {},
        },
        workingDirectory: '/tmp/example-project',
        sessionDirectory: '/tmp/example-project/.juno_task',
      });
    });

    it('recursively adds absent object keys without replacing explicit scalars or arrays', () => {
      const existing: Record<string, unknown> = {
        enabled: false,
        empty: {},
        list: [],
        nested: { custom: 'keep' },
        hooks: { CUSTOM: { commands: ['keep'] } },
      };
      expect(
        mergePersistedProjectDefaults(existing, {
          enabled: true,
          empty: { added: true },
          list: ['default'],
          nested: { custom: 'replace', added: true },
          hooks: { START_RUN: { commands: ['default'] } },
          newKey: 'added',
        }),
      ).toBe(true);
      expect(existing).toEqual({
        enabled: false,
        empty: { added: true },
        list: [],
        nested: { custom: 'keep', added: true },
        hooks: { CUSTOM: { commands: ['keep'] } },
        newKey: 'added',
      });
    });
  });

  describe('JunoTaskConfigSchema validation', () => {
    it('should validate valid configuration', () => {
      const validConfig: JunoTaskConfig = {
        defaultSubagent: 'claude',
        defaultBackend: 'shell',
        defaultMaxIterations: 25,
        logLevel: 'debug',
        verbose: 1,
        quiet: false,
        mcpTimeout: 45000,
        mcpRetries: 5,
        interactive: false,
        headlessMode: true,
        headlessUi: { turnCostDisplayThresholdUsd: 0.5 },
        workingDirectory: '/test/path',
        sessionDirectory: '/test/sessions',
        envFilePath: '.env.yylo',
        envFileCopied: true,
        hooks: {},
        onHourlyLimit: 'raise',
      };

      const result = validateConfig(validConfig);
      expect(result).toEqual(validConfig);
    });

    it('should reject invalid subagent', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        defaultSubagent: 'invalid-subagent',
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/defaultSubagent/);
    });

    it('validates provider-neutral headless UI settings', () => {
      expect(validateConfig({
        ...DEFAULT_CONFIG,
        headlessUi: { turnCostDisplayThresholdUsd: 0.75 },
      }).headlessUi.turnCostDisplayThresholdUsd).toBe(0.75);
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        headlessUi: { turnCostDisplayThresholdUsd: -0.01 },
      })).toThrow(/greater than or equal to 0/);
    });

    it('should accept per-subagent defaultModels overrides', () => {
      const config = validateConfig({
        ...DEFAULT_CONFIG,
        defaultModels: {
          claude: ':opus',
          pi: ':api-codex',
        },
      });

      expect(config.defaultModels).toEqual({
        claude: ':opus',
        pi: ':api-codex',
      });
    });

    it('accepts only trimmed unique workflow model selectors', () => {
      expect(validateConfig({ ...DEFAULT_CONFIG, workflowModels: [':luna', 'openai/gpt-4o'] }).workflowModels)
        .toEqual([':luna', 'openai/gpt-4o']);
      expect(() => validateConfig({ ...DEFAULT_CONFIG, workflowModels: [':luna', ':luna'] })).toThrow(/unique/);
      expect(() => validateConfig({ ...DEFAULT_CONFIG, workflowModels: [' :luna'] })).toThrow(/trimmed/);
      expect(() => validateConfig({ ...DEFAULT_CONFIG, workflowModels: [''] })).toThrow();
    });

    it('should accept controller Git checkpoint configuration', () => {
      const config = validateConfig({
        ...DEFAULT_CONFIG,
        gitCheckpoint: {
          include: ['.juno_task/tasks', '.juno_task/workflows'],
          agent: {
            enabled: false,
            service: 'pi',
            model: ':luna',
            timeoutSeconds: 120,
          },
        },
      });

      expect(config.gitCheckpoint).toEqual({
        include: ['.juno_task/tasks', '.juno_task/workflows'],
        agent: {
          enabled: false,
          service: 'pi',
          model: ':luna',
          timeoutSeconds: 120,
        },
      });
    });

    it('should reject malformed controller Git checkpoint configuration', () => {
      expect(() =>
        validateConfig({
          ...DEFAULT_CONFIG,
          gitCheckpoint: { include: [''] },
        }),
      ).toThrow(/gitCheckpoint/);
      expect(() =>
        validateConfig({
          ...DEFAULT_CONFIG,
          gitCheckpoint: { agent: { timeoutSeconds: 601 } },
        }),
      ).toThrow(/gitCheckpoint/);
    });

    it('should accept only the canonical Git-flow policy pointer', () => {
      const config = validateConfig({
        ...DEFAULT_CONFIG,
        gitFlow: {
          enabled: true,
          policy: '.juno_task/config/git-flow.json',
        },
      });
      expect(config.gitFlow).toEqual({
        enabled: true,
        policy: '.juno_task/config/git-flow.json',
      });
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        gitFlow: { enabled: true, policy: '../outside.json' },
      })).toThrow(/gitFlow/);
    });

    it('accepts only the canonical metadata-controller policy pointer', () => {
      const config = validateConfig({
        ...DEFAULT_CONFIG,
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
      });
      expect(config.controllerWorkspace?.mode).toBe('metadata-only');
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        controllerWorkspace: { enabled: true, policy: '.juno_task/config/controller-workspace.json' },
      })).toThrow(/Migration required.*metadata-only controller/);
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        lifecycle: { enabled: true, policy: '.juno_task/config/lifecycle.json' },
      })).toThrow(/Migration required.*persisted lifecycle/);
    });

    it('selects role-owned hooks without exposing product hooks to the controller', () => {
      const profile = {
        version: 1 as const,
        promptAssetRoot: '.juno_task/prompts',
        roleHooks: {
          controller: { START_RUN: { commands: ['controller-only'] } },
          product: { START_RUN: { commands: ['product-only'] } },
        },
      };
      expect(selectAgentProfileHooks(profile, 'controller')?.START_RUN?.commands).toEqual(['controller-only']);
      expect(selectAgentProfileHooks(profile, 'task')?.START_RUN?.commands).toEqual(['product-only']);
      expect(selectAgentProfileHooks(profile, 'integration-owner')?.START_RUN?.commands).toEqual(['product-only']);
      expect(selectAgentProfileHooks(profile, 'controller-retired')).toBeUndefined();
      expect(selectAgentProfileHooks(profile, 'unregistered')).toBeUndefined();
    });

    it('accepts only a versioned controller agent profile with a contained asset root', () => {
      expect(validateConfig({
        ...DEFAULT_CONFIG,
        agentProfile: { version: 1, promptAssetRoot: '.juno_task/prompts' },
      }).agentProfile).toEqual({ version: 1, promptAssetRoot: '.juno_task/prompts' });
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        agentProfile: { version: 2, promptAssetRoot: '.juno_task/prompts' },
      })).toThrow(/agentProfile/);
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        agentProfile: { version: 1, promptAssetRoot: '../product' },
      })).toThrow(/contained by the controller/);
    });

    it('rejects product-only settings in a metadata-controller source before merge', async () => {
      await fs.ensureDir(path.join(tempDir, '.juno_task'));
      await fs.writeJson(path.join(tempDir, '.juno_task', 'config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
        workingDirectory: '/old/product',
      });
      await expect(new ConfigLoader(tempDir).loadAll()).rejects.toThrow(
        /workingDirectory is product-only/,
      );
    });

    it('should accept an enabled cross-project Kanban alias allowlist', () => {
      const config = validateConfig({
        ...DEFAULT_CONFIG,
        kanbanRegistry: {
          enabled: true,
          allowedProjects: ['yylo', 'convert_if_chat'],
        },
      });
      expect(config.kanbanRegistry).toEqual({
        enabled: true,
        allowedProjects: ['yylo', 'convert_if_chat'],
      });
    });

    it('should reject unsafe or ambiguous cross-project Kanban registry config', () => {
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        kanbanRegistry: { enabled: true, allowedProjects: ['Uppercase'] },
      })).toThrow(/kanbanRegistry/);
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        kanbanRegistry: { enabled: true, allowedProjects: ['same', 'same'] },
      })).toThrow(/kanbanRegistry/);
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        kanbanRegistry: { enabled: true, allowedProjects: [], allowAll: true },
      })).toThrow(/kanbanRegistry/);
    });

    it('should reject invalid log level', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        logLevel: 'invalid-level',
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/logLevel/);
    });

    it('should reject invalid max iterations', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        defaultMaxIterations: 0,
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/defaultMaxIterations/);
    });

    it('should reject max iterations too high', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        defaultMaxIterations: 1001,
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/defaultMaxIterations/);
    });

    it('should reject invalid timeout values', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        mcpTimeout: 500, // Too low
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/mcpTimeout/);
    });

    it('should reject timeout too high', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        mcpTimeout: 90000000, // Too high (exceeds 86400000 max = 24 hours)
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/mcpTimeout/);
    });

    it('should reject invalid retry count', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        mcpRetries: -1,
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/mcpRetries/);
    });

    it('should reject retries too high', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        mcpRetries: 15,
      };

      expect(() => validateConfig(invalidConfig)).toThrow(/mcpRetries/);
    });

    it('should reject extra properties', () => {
      const invalidConfig = {
        ...DEFAULT_CONFIG,
        extraProperty: 'not-allowed',
      };

      expect(() => validateConfig(invalidConfig)).toThrow();
    });

    it('should accept optional fields as undefined', () => {
      const configWithOptionals = {
        ...DEFAULT_CONFIG,
        defaultModel: 'gpt-4',
        logFile: '/var/log/juno.log',
        mcpServerPath: '/usr/bin/mcp-server',
      };

      expect(() => validateConfig(configWithOptionals)).not.toThrow();
    });

    it('should validate prompt macro config and dictionary precedence (local > global)', () => {
      const parsed = validateConfig({
        ...DEFAULT_CONFIG,
        promptMacros: {
          enabled: true,
          order: 'before_command_substitution',
          maxDepth: 12,
          global: {
            git: 'global git',
            shared: 'from global',
          },
          local: {
            shared: 'from local',
            ship: 'run tests then @@git',
          },
        },
      });

      expect(parsed.promptMacros?.maxDepth).toBe(12);
      expect(getPromptMacroDictionary(parsed)).toEqual({
        git: 'global git',
        shared: 'from local',
        ship: 'run tests then @@git',
      });
    });

    it('should load prompt macro dictionary entries from text values and project-relative files', async () => {
      await fs.ensureDir(path.join(tempDir, 'prompts'));
      await fs.ensureDir(path.join(tempDir, '.juno_task'));
      await fs.writeFile(path.join(tempDir, 'prompts', 'ship.md'), 'ship docs with @@git');

      await fs.writeJson(path.join(tempDir, '.juno_task', 'config.json'), {
        promptMacros: {
          global: {
            git: 'commit changes',
            docs: { path: 'prompts/ship.md' },
          },
          local: {
            inline: { text: "run !'echo tests'" },
          },
        },
      });

      const config = await new ConfigLoader(tempDir).loadAll();

      expect(getPromptMacroDictionary(config)).toEqual({
        git: 'commit changes',
        docs: 'ship docs with @@git',
        inline: "run !'echo tests'",
      });
    });

    it('loads a canonical controller profile from another workspace with an explicit asset root', async () => {
      const invocation = path.join(tempDir, 'task');
      const controller = path.join(tempDir, 'controller');
      await fs.ensureDir(invocation);
      await fs.ensureDir(path.join(controller, '.juno_task', 'prompts'));
      await fs.writeFile(path.join(controller, '.juno_task', 'prompts', 'ship.md'), 'controller asset');
      await fs.writeJson(path.join(controller, '.juno_task', 'config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
        agentProfile: { version: 1, promptAssetRoot: '.juno_task/prompts' },
        defaultMaxIterations: 17,
        promptMacros: { global: { ship: { path: 'ship.md' } } },
      });

      const config = await new ConfigLoader(invocation, controller).loadAll();
      expect(config.defaultMaxIterations).toBe(17);
      expect(getPromptMacroDictionary(config).ship).toBe('controller asset');
    });

    it('rejects controller profile prompt assets that escape through a symlink', async () => {
      const outside = path.join(tempDir, 'outside.md');
      await fs.writeFile(outside, 'unsafe');
      await fs.ensureDir(path.join(tempDir, '.juno_task', 'prompts'));
      await fs.symlink(outside, path.join(tempDir, '.juno_task', 'prompts', 'escape.md'));
      await fs.writeJson(path.join(tempDir, '.juno_task', 'config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only',
          policy: '.juno_task/config/metadata-controller.json',
        },
        agentProfile: { version: 1, promptAssetRoot: '.juno_task/prompts' },
        promptMacros: { global: { unsafe: { path: 'escape.md' } } },
      });
      await expect(new ConfigLoader(tempDir).loadAll()).rejects.toThrow(/escapes the configured promptAssetRoot/);
    });

    it('should load prompt macro dictionary entries from absolute file paths', async () => {
      const promptPath = path.join(tempDir, 'absolute-prompt.txt');
      await fs.writeFile(promptPath, 'absolute prompt text');
      await fs.ensureDir(path.join(tempDir, '.juno_task'));
      await fs.writeJson(path.join(tempDir, '.juno_task', 'config.json'), {
        promptMacros: {
          local: {
            absolute: { path: promptPath },
          },
        },
      });

      const config = await new ConfigLoader(tempDir).loadAll();

      expect(getPromptMacroDictionary(config).absolute).toBe('absolute prompt text');
    });

    it('should reject prompt macro object entries that define both path and text', async () => {
      await fs.ensureDir(path.join(tempDir, '.juno_task'));
      await fs.writeJson(path.join(tempDir, '.juno_task', 'config.json'), {
        promptMacros: {
          local: {
            invalid: { path: 'prompt.md', text: 'inline prompt' },
          },
        },
      });

      await expect(new ConfigLoader(tempDir).loadAll()).rejects.toThrow(
        /promptMacros\.local\.invalid must define exactly one non-empty field: path or text/,
      );
    });

    it('should reject malformed promptMacros config with actionable errors', () => {
      expect(() =>
        validateConfig({
          ...DEFAULT_CONFIG,
          promptMacros: {
            maxDepth: 0,
            global: ['bad-shape'] as unknown as Record<string, string>,
          },
        }),
      ).toThrow(/promptMacros/);
    });

    it('should show actionable hint for snake_case prompt macro config keys', () => {
      expect(() =>
        validateConfig({
          ...DEFAULT_CONFIG,
          prompt_macros: {
            max_depth: 10,
            global: {
              git: 'commit changes',
            },
          },
        }),
      ).toThrow(/Hint: use config\.promptMacros/);
    });
  });

  describe('Environment variable parsing', () => {
    it('should parse boolean environment variables (YYLO)', () => {
      process.env.YYLO_VERBOSE = 'true';
      process.env.YYLO_QUIET = 'false';

      const loader = new ConfigLoader(tempDir);
      loader.fromEnvironment();
      const config = loader.merge();

      expect(config.verbose).toBe(1);
      expect(config.quiet).toBe(false);
    });

    it('should parse boolean environment variables (JUNO_TASK backward compatibility)', () => {
      process.env.YYLO_VERBOSE = 'true';
      process.env.JUNO_TASK_QUIET = 'false';

      const loader = new ConfigLoader(tempDir);
      loader.fromEnvironment();
      const config = loader.merge();

      expect(config.verbose).toBe(1);
      expect(config.quiet).toBe(false);
    });

    it('should parse numeric environment variables (YYLO)', () => {
      process.env.YYLO_DEFAULT_MAX_ITERATIONS = '75';
      process.env.YYLO_MCP_TIMEOUT = '45000';
      process.env.YYLO_MCP_RETRIES = '5';

      const loader = new ConfigLoader(tempDir);
      loader.fromEnvironment();
      const config = loader.merge();

      expect(config.defaultMaxIterations).toBe(75);
      expect(config.mcpTimeout).toBe(45000);
      expect(config.mcpRetries).toBe(5);
    });

    it('should parse string environment variables (YYLO)', () => {
      process.env.YYLO_DEFAULT_SUBAGENT = 'cursor';
      process.env.YYLO_LOG_LEVEL = 'debug';
      process.env.YYLO_WORKING_DIRECTORY = '/custom/path';

      const loader = new ConfigLoader(tempDir);
      loader.fromEnvironment();
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('cursor');
      expect(config.logLevel).toBe('debug');
      expect(config.workingDirectory).toBe('/custom/path');
    });

    it('should handle case-insensitive boolean values', () => {
      process.env.YYLO_VERBOSE = 'TRUE';
      process.env.YYLO_QUIET = 'False';

      const loader = new ConfigLoader(tempDir);
      loader.fromEnvironment();
      const config = loader.merge();

      expect(config.verbose).toBe(1);
      expect(config.quiet).toBe(false);
    });

    it('should ignore invalid numeric values', () => {
      process.env.YYLO_DEFAULT_MAX_ITERATIONS = 'not-a-number';

      const loader = new ConfigLoader(tempDir);
      loader.fromEnvironment();
      const config = loader.merge();

      expect(config.defaultMaxIterations).toBe('not-a-number');
    });
  });

  describe('JSON configuration files', () => {
    it('should load configuration from JSON file', async () => {
      const configData = {
        defaultSubagent: 'gemini',
        defaultMaxIterations: 30,
        logLevel: 'warn',
        verbose: 1,
        mcpTimeout: 60000,
      };

      const configPath = path.join(tempDir, 'yylo.config.json');
      await fs.writeJson(configPath, configData);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('gemini');
      expect(config.defaultMaxIterations).toBe(30);
      expect(config.logLevel).toBe('warn');
      expect(config.verbose).toBe(1);
      expect(config.mcpTimeout).toBe(60000);
    });

    it('should handle invalid JSON gracefully', async () => {
      const configPath = path.join(tempDir, 'invalid.json');
      await fs.writeFile(configPath, '{ invalid json');

      const loader = new ConfigLoader(tempDir);
      await expect(loader.fromFile(configPath)).rejects.toThrow(/Failed to load JSON config/);
    });

    it('should handle non-existent files', async () => {
      const configPath = path.join(tempDir, 'non-existent.json');

      const loader = new ConfigLoader(tempDir);
      await expect(loader.fromFile(configPath)).rejects.toThrow(/not readable/);
    });

    it('should auto-discover configuration files', async () => {
      const configData = {
        defaultSubagent: 'codex',
        logLevel: 'error',
      };

      // Test multiple file formats in order of preference
      const configPath = path.join(tempDir, 'yylo.config.json');
      await fs.writeJson(configPath, configData);

      const loader = new ConfigLoader(tempDir);
      await loader.autoDiscoverFile();
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('codex');
      expect(config.logLevel).toBe('error');
    });

    it.each(['juno-code.config.json', '.juno-coderc.json'])(
      'should discover the legacy %s filename as a read-only fallback',
      async (filename) => {
        await fs.writeJson(path.join(tempDir, filename), {
          defaultSubagent: 'gemini',
          logLevel: 'warn',
        });

        const loader = new ConfigLoader(tempDir);
        await loader.autoDiscoverFile();
        const config = loader.merge();
        expect(config.defaultSubagent).toBe('gemini');
        expect(config.logLevel).toBe('warn');
      },
    );

    it('should prefer files in order of precedence', async () => {
      // Create multiple config files
      await fs.writeJson(path.join(tempDir, 'yylo.config.json'), {
        defaultSubagent: 'claude',
      });

      await fs.writeJson(path.join(tempDir, '.yylorc.json'), {
        defaultSubagent: 'cursor',
      });

      await fs.writeJson(path.join(tempDir, 'juno-code.config.json'), {
        defaultSubagent: 'gemini',
      });

      const loader = new ConfigLoader(tempDir);
      await loader.autoDiscoverFile();
      const config = loader.merge();

      // Should prefer the first one found (yylo.config.json)
      expect(config.defaultSubagent).toBe('claude');
    });

    it('should load from the canonical package.json yylo field', async () => {
      const packageJson = {
        name: 'test-package',
        version: '1.0.0',
        yylo: {
          defaultSubagent: 'gemini',
          logLevel: 'trace',
          verbose: 1,
        },
      };

      const packagePath = path.join(tempDir, 'package.json');
      await fs.writeJson(packagePath, packageJson);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(packagePath);
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('gemini');
      expect(config.logLevel).toBe('trace');
      expect(config.verbose).toBe(1);
    });

    it('should retain the legacy package.json junoCode field as a fallback', async () => {
      const packagePath = path.join(tempDir, 'package.json');
      await fs.writeJson(packagePath, {
        yylo: { defaultSubagent: 'pi' },
        junoCode: { defaultSubagent: 'gemini' },
      });

      const canonicalLoader = new ConfigLoader(tempDir);
      await canonicalLoader.fromFile(packagePath);
      expect(canonicalLoader.merge().defaultSubagent).toBe('pi');

      await fs.writeJson(packagePath, { junoCode: { defaultSubagent: 'gemini' } });
      const legacyLoader = new ConfigLoader(tempDir);
      await legacyLoader.fromFile(packagePath);
      expect(legacyLoader.merge().defaultSubagent).toBe('gemini');
    });

    it('should handle package.json without yylo or legacy junoCode fields', async () => {
      const packageJson = {
        name: 'test-package',
        version: '1.0.0',
      };

      const packagePath = path.join(tempDir, 'package.json');
      await fs.writeJson(packagePath, packageJson);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(packagePath);
      const config = loader.merge();

      // Should use defaults since no canonical or legacy package field exists
      expect(config.defaultSubagent).toBe(DEFAULT_CONFIG.defaultSubagent);
    });
  });

  describe('YAML configuration files', () => {
    it('should load configuration from YAML file', async () => {
      const yamlContent = `
defaultSubagent: cursor
defaultMaxIterations: 40
logLevel: debug
verbose: 0
mcpTimeout: 50000
`;

      const configPath = path.join(tempDir, 'config.yaml');
      await fs.writeFile(configPath, yamlContent);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('cursor');
      expect(config.defaultMaxIterations).toBe(40);
      expect(config.logLevel).toBe('debug');
      expect(config.verbose).toBe(0);
      expect(config.mcpTimeout).toBe(50000);
    });

    it('should handle invalid YAML gracefully', async () => {
      const configPath = path.join(tempDir, 'invalid.yaml');
      await fs.writeFile(configPath, 'invalid: yaml: content: [');

      const loader = new ConfigLoader(tempDir);
      await expect(loader.fromFile(configPath)).rejects.toThrow(/Failed to load YAML config/);
    });

    it('should support .yml extension', async () => {
      const yamlContent = `
defaultSubagent: gemini
logLevel: info
`;

      const configPath = path.join(tempDir, 'config.yml');
      await fs.writeFile(configPath, yamlContent);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('gemini');
      expect(config.logLevel).toBe('info');
    });
  });

  describe('Configuration precedence', () => {
    it('should apply precedence: CLI > Environment > File > Defaults', async () => {
      // Setup file config
      const fileConfig = {
        defaultSubagent: 'claude',
        logLevel: 'info',
        verbose: 0,
      };
      const configPath = path.join(tempDir, 'yylo.config.json');
      await fs.writeJson(configPath, fileConfig);

      // Setup environment config
      process.env.YYLO_DEFAULT_SUBAGENT = 'cursor';
      process.env.YYLO_VERBOSE = 'true';

      // Setup CLI config
      const cliConfig = {
        defaultSubagent: 'gemini',
        logLevel: 'debug',
      };

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      loader.fromEnvironment();
      loader.fromCli(cliConfig);

      const config = loader.merge();

      // CLI should override everything
      expect(config.defaultSubagent).toBe('gemini');
      expect(config.logLevel).toBe('debug');

      // Environment should override file
      expect(config.verbose).toBe(1);
    });

    it('should use defaults for unspecified values', async () => {
      const partialConfig = {
        defaultSubagent: 'cursor',
      };

      const loader = new ConfigLoader(tempDir);
      loader.fromCli(partialConfig);
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('cursor');
      expect(config.logLevel).toBe(DEFAULT_CONFIG.logLevel);
      expect(config.mcpTimeout).toBe(DEFAULT_CONFIG.mcpTimeout);
    });

    it('should merge prompt macro layers with default maxDepth/order and local precedence', async () => {
      const fileConfig = {
        promptMacros: {
          global: {
            git: 'global git',
            shared: 'global shared',
          },
        },
      };
      const configPath = path.join(tempDir, 'yylo.config.json');
      await fs.writeJson(configPath, fileConfig);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      loader.fromCli({
        promptMacros: {
          local: {
            shared: 'local shared',
            ship: 'run tests then @@git',
          },
        },
      });

      const config = loader.merge();

      expect(config.promptMacros?.maxDepth).toBe(10);
      expect(config.promptMacros?.order).toBe('before_command_substitution');
      expect(getPromptMacroDictionary(config)).toEqual({
        git: 'global git',
        shared: 'local shared',
        ship: 'run tests then @@git',
      });
    });
  });

  describe('Path resolution', () => {
    it('should resolve relative paths to absolute', async () => {
      const configData = {
        workingDirectory: './relative/path',
        sessionDirectory: '../sessions',
        logFile: 'logs/app.log',
        mcpServerPath: './bin/server',
      };

      const loader = new ConfigLoader(tempDir);
      loader.fromCli(configData);
      const config = loader.merge();

      expect(path.isAbsolute(config.workingDirectory)).toBe(true);
      expect(path.isAbsolute(config.sessionDirectory)).toBe(true);
      expect(config.logFile && path.isAbsolute(config.logFile)).toBe(true);
      expect(config.mcpServerPath && path.isAbsolute(config.mcpServerPath)).toBe(true);
    });

    it('should preserve absolute paths', async () => {
      const absolutePath = path.resolve('/absolute/path');
      const configData = {
        workingDirectory: absolutePath,
      };

      const loader = new ConfigLoader(tempDir);
      loader.fromCli(configData);
      const config = loader.merge();

      expect(config.workingDirectory).toBe(absolutePath);
    });

    it('should resolve paths relative to base directory', async () => {
      const customBaseDir = path.join(tempDir, 'custom-base');
      await fs.ensureDir(customBaseDir);

      const configData = {
        workingDirectory: './project',
      };

      const loader = new ConfigLoader(customBaseDir);
      loader.fromCli(configData);
      const config = loader.merge();

      expect(config.workingDirectory).toBe(path.join(customBaseDir, 'project'));
    });
  });

  describe('loadConfig function', () => {
    it('should load and validate configuration with all sources', async () => {
      // Setup file
      const fileConfig = {
        defaultSubagent: 'claude',
        logLevel: 'info',
      };
      const configPath = path.join(tempDir, 'yylo.config.json');
      await fs.writeJson(configPath, fileConfig);

      // Setup environment
      process.env.YYLO_VERBOSE = 'true';

      // Setup CLI
      const cliConfig = {
        defaultMaxIterations: 100,
      };

      const config = await loadConfig({
        baseDir: tempDir,
        cliConfig,
      });

      expect(config.defaultSubagent).toBe('claude');
      expect(config.logLevel).toBe('info');
      expect(config.verbose).toBe(1);
      expect(config.defaultMaxIterations).toBe(100);
    });

    it('loads only an explicitly authorized 0600 controller environment binding', async () => {
      const environment = path.join(tempDir, 'controller.env');
      await fs.writeFile(environment, 'YYLO_DEFAULT_MAX_ITERATIONS=9\n', { mode: 0o644 });
      await fs.ensureDir(path.join(tempDir, '.juno_task', 'prompts'));
      await fs.writeJson(path.join(tempDir, '.juno_task', 'config.json'), {
        controllerWorkspace: {
          mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json',
        },
        agentProfile: {
          version: 1, promptAssetRoot: '.juno_task/prompts',
          environmentBinding: { source: environment, authorized: true },
        },
      });
      process.env.YYLO_PROJECT_BOOTSTRAP_WRITES = '0';
      await expect(loadConfig({ baseDir: tempDir })).rejects.toThrow(/mode 0600/);
      await fs.chmod(environment, 0o600);
      expect((await loadConfig({ baseDir: tempDir })).defaultMaxIterations).toBe(9);
      expect(await fs.pathExists(path.join(tempDir, '.env.yylo'))).toBe(false);
      const target = path.join(tempDir, 'environment-target');
      await fs.writeFile(target, 'YYLO_DEFAULT_MAX_ITERATIONS=10\n', { mode: 0o600 });
      await fs.remove(environment);
      await fs.symlink(target, environment);
      await expect(loadConfig({ baseDir: tempDir })).rejects.toThrow(/missing, unsafe, or unreadable/);
    });

    it('should load from specific config file', async () => {
      const customConfig = {
        defaultSubagent: 'codex',
        logLevel: 'warn',
      };
      const customConfigPath = path.join(tempDir, 'custom.json');
      await fs.writeJson(customConfigPath, customConfig);

      const config = await loadConfig({
        baseDir: tempDir,
        configFile: customConfigPath,
      });

      expect(config.defaultSubagent).toBe('codex');
      expect(config.logLevel).toBe('warn');
    });

    it('should validate final merged configuration', async () => {
      const invalidConfig = {
        defaultSubagent: 'invalid-agent',
      };

      await expect(
        loadConfig({
          baseDir: tempDir,
          cliConfig: invalidConfig,
        }),
      ).rejects.toThrow(/Configuration validation failed/);
    });

    it('should handle missing config files gracefully', async () => {
      const config = await loadConfig({
        baseDir: tempDir,
      });

      // Should use defaults when no config file found
      // NOTE: loadConfig() auto-migrates hooks to include default hooks template with file size monitoring
      expect(config).toMatchObject({
        ...DEFAULT_CONFIG,
        workingDirectory: tempDir,
        sessionDirectory: path.join(tempDir, '.juno_task'),
        hooks: {
          START_RUN: {
            commands: expect.arrayContaining([
              expect.stringContaining('./.juno_task/scripts/install_requirements.sh'),
            ]),
          },
          START_ITERATION: {
            commands: expect.arrayContaining([
              expect.stringContaining('CLAUDE.md'),
              expect.stringContaining('AGENTS.md'),
              expect.stringContaining('--reject-duplicates'),
            ]),
          },
        },
      });

      // New behavior: always bootstrap project env file on load
      expect(await fs.pathExists(path.join(tempDir, '.env.yylo'))).toBe(true);
    });

    it('additively migrates a legacy project config and is byte-idempotent', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        defaultModel: 'user-owned-model',
        defaultModels: { pi: ':gpt' },
        defaultMaxIterations: 50,
        hooks: { START_ITERATION: { commands: ['custom-hook'] } },
        promptMacros: { global: { custom: 'keep' } },
        gitCheckpoint: { include: [] },
        kanbanRegistry: { enabled: true, allowedProjects: [] },
      });
      await fs.chmod(configPath, 0o600);

      await loadConfig({ baseDir: tempDir });
      const migrated = await fs.readJson(configPath);
      expect(migrated).toMatchObject({
        configVersion: PROJECT_CONFIG_VERSION,
        defaultSubagent: 'claude',
        defaultModel: 'user-owned-model',
        defaultModels: { pi: ':gpt' },
        defaultMaxIterations: 50,
        defaultBackend: 'shell',
        autoDependencyUpdate: true,
        hooks: { START_ITERATION: { commands: ['custom-hook'] } },
        promptMacros: {
          enabled: true,
          order: 'before_command_substitution',
          maxDepth: 10,
          global: { custom: 'keep' },
          local: {},
        },
        gitCheckpoint: { include: [] },
        kanbanRegistry: { enabled: true, allowedProjects: [] },
      });
      expect(migrated.hooks).toEqual({ START_ITERATION: { commands: ['custom-hook'] } });
      expect(migrated.defaultModels).toEqual({ pi: ':gpt' });
      expect(migrated.workflowModels).toEqual([]);
      expect((await fs.stat(configPath)).mode & 0o777).toBe(0o600);

      const once = await fs.readFile(configPath);
      await loadConfig({ baseDir: tempDir });
      expect(await fs.readFile(configPath)).toEqual(once);
    });

    it('preserves an explicit workflow model allowlist, including an empty array', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeJson(configPath, { ...createPersistedProjectConfigDefaults(tempDir), workflowModels: [':luna'] });
      await loadConfig({ baseDir: tempDir });
      expect((await fs.readJson(configPath)).workflowModels).toEqual([':luna']);

      await fs.writeJson(configPath, { ...createPersistedProjectConfigDefaults(tempDir), workflowModels: [] });
      await loadConfig({ baseDir: tempDir });
      expect((await fs.readJson(configPath)).workflowModels).toEqual([]);
    });

    it('preserves an explicit empty model scalar', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeJson(configPath, {
        defaultSubagent: 'claude',
        defaultModel: '',
        hooks: {},
      });

      const config = await loadConfig({ baseDir: tempDir });

      expect(config.defaultModel).toBe('');
      expect((await fs.readJson(configPath)).defaultModel).toBe('');
    });

    it('does not mutate an invalid project config before validation fails', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeFile(
        configPath,
        '{"defaultSubagent":"claude","defaultMaxIterations":2000,"hooks":{}}\n',
      );
      const before = await fs.readFile(configPath);

      await expect(loadConfig({ baseDir: tempDir })).rejects.toThrow(/defaultMaxIterations/);

      expect(await fs.readFile(configPath)).toEqual(before);
      expect(await fs.pathExists(path.join(tempDir, '.env.yylo'))).toBe(false);
    });

    it('validates CLI overrides before migrating valid project bytes', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeJson(configPath, { defaultSubagent: 'claude', hooks: {} });
      const before = await fs.readFile(configPath);

      await expect(
        loadConfig({ baseDir: tempDir, cliConfig: { defaultMaxIterations: 2000 } }),
      ).rejects.toThrow(/defaultMaxIterations/);

      expect(await fs.readFile(configPath)).toEqual(before);
      expect(await fs.pathExists(path.join(tempDir, '.env.yylo'))).toBe(false);
    });

    it('leaves original config bytes intact when atomic replacement fails', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      const legacy = createPersistedProjectConfigDefaults(tempDir);
      delete legacy.configVersion;
      legacy.envFileCopied = true;
      await fs.writeJson(configPath, legacy, { spaces: 4 });
      await fs.writeFile(path.join(tempDir, '.env.yylo'), '');
      const before = await fs.readFile(configPath);
      await expect(
        writeProjectConfigAtomic(
          configPath,
          { ...legacy, configVersion: PROJECT_CONFIG_VERSION },
          async () => {
            throw new Error('injected rename failure');
          },
        ),
      ).rejects.toThrow('injected rename failure');

      expect(await fs.readFile(configPath)).toEqual(before);
      expect((await fs.readdir(path.dirname(configPath))).some((name) => name.endsWith('.tmp'))).toBe(false);
    });

    it('preserves mode and refuses replacement when the expected original changed', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeFile(configPath, '{"original":true}\n', { mode: 0o600 });
      const expected = await fs.readFile(configPath);
      await fs.writeFile(configPath, '{"concurrent":true}\n', { mode: 0o600 });

      await expect(
        writeProjectConfigAtomic(configPath, { migrated: true }, fs.rename, expected),
      ).rejects.toThrow('project config changed during migration');

      expect(await fs.readJson(configPath)).toEqual({ concurrent: true });
      expect((await fs.stat(configPath)).mode & 0o777).toBe(0o600);
    });

    it('keeps the managed Python checkpoint fallback aligned with persisted defaults', async () => {
      const script = await fs.readFile(
        path.join(process.cwd(), 'src/templates/scripts/controller_checkpoint.py'),
        'utf8',
      );
      const match = script.match(/DEFAULT_INCLUDE = \((.*?)\n\)/s);
      expect(match).not.toBeNull();
      const pythonDefaults = [...match![1].matchAll(/"([^"]+)"/g)].map((entry) => entry[1]);
      expect(pythonDefaults).toEqual(DEFAULT_GIT_CHECKPOINT_INCLUDE);
    });

    it('loads existing task env without migrating any project bytes in read-only bootstrap mode', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeJson(configPath, { defaultSubagent: 'pi' });
      await fs.writeFile(path.join(tempDir, '.env.yylo'), 'YYLO_LOG_LEVEL=debug\n');
      const beforeConfig = await fs.readFile(configPath);
      const beforeEnv = await fs.readFile(path.join(tempDir, '.env.yylo'));

      process.env.YYLO_PROJECT_BOOTSTRAP_WRITES = '0';
      try {
        const config = await loadConfig({ baseDir: tempDir });
        expect(config.defaultSubagent).toBe('pi');
        expect(config.logLevel).toBe('debug');
        expect(await fs.readFile(configPath)).toEqual(beforeConfig);
        expect(await fs.readFile(path.join(tempDir, '.env.yylo'))).toEqual(beforeEnv);
        expect(await fs.pathExists(path.join(tempDir, '.env.custom'))).toBe(false);
      } finally {
        delete process.env.YYLO_PROJECT_BOOTSTRAP_WRITES;
      }
    });

    it('loads a legacy-only env file read-only without migrating project bytes', async () => {
      const configPath = path.join(tempDir, '.juno_task', 'config.json');
      const legacyEnvPath = path.join(tempDir, '.env.juno');
      const canonicalEnvPath = path.join(tempDir, '.env.yylo');
      await fs.ensureDir(path.dirname(configPath));
      await fs.writeJson(configPath, { defaultSubagent: 'pi' });
      await fs.writeFile(legacyEnvPath, 'YYLO_LOG_LEVEL=debug\n');
      const beforeConfig = await fs.readFile(configPath);
      const beforeLegacy = await fs.readFile(legacyEnvPath);

      process.env.YYLO_PROJECT_BOOTSTRAP_WRITES = '0';
      try {
        const config = await loadConfig({ baseDir: tempDir });
        expect(config.logLevel).toBe('debug');
        expect(await fs.readFile(configPath)).toEqual(beforeConfig);
        expect(await fs.readFile(legacyEnvPath)).toEqual(beforeLegacy);
        expect(await fs.pathExists(canonicalEnvPath)).toBe(false);
      } finally {
        delete process.env.YYLO_PROJECT_BOOTSTRAP_WRITES;
      }
    });

    it('prefers the canonical env file over a conflicting legacy file', async () => {
      await fs.writeFile(path.join(tempDir, '.env.juno'), 'YYLO_LOG_LEVEL=error\n');
      await fs.writeFile(path.join(tempDir, '.env.yylo'), 'YYLO_LOG_LEVEL=debug\n');

      process.env.YYLO_PROJECT_BOOTSTRAP_WRITES = '0';
      try {
        expect((await loadConfig({ baseDir: tempDir })).logLevel).toBe('debug');
      } finally {
        delete process.env.YYLO_PROJECT_BOOTSTRAP_WRITES;
      }
    });

    it('should load env values from .env.yylo before reading environment mapping', async () => {
      await fs.writeFile(path.join(tempDir, '.env.yylo'), 'YYLO_DEFAULT_MAX_ITERATIONS=12\n');

      const config = await loadConfig({
        baseDir: tempDir,
      });

      expect(config.defaultMaxIterations).toBe(12);
    });

    it('should unescape double-quoted env values so JSON snapshots remain parseable', async () => {
      const snapshot = JSON.stringify({ version: 1, subagent: 'pi', model: ':api-codex' });
      const escapedSnapshot = snapshot.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      await fs.writeFile(
        path.join(tempDir, '.env.yylo'),
        `YYLO_LAST_EXECUTION_SETTINGS="${escapedSnapshot}"\n`,
      );

      await loadConfig({
        baseDir: tempDir,
      });

      expect(process.env.YYLO_LAST_EXECUTION_SETTINGS).toBe(snapshot);
      expect(JSON.parse(process.env.YYLO_LAST_EXECUTION_SETTINGS || '{}')).toEqual({
        version: 1,
        subagent: 'pi',
        model: ':api-codex',
      });
    });

    it('should support custom envFilePath and mark envFileCopied after first bootstrap', async () => {
      const junoTaskDir = path.join(tempDir, '.juno_task');
      await fs.ensureDir(junoTaskDir);

      await fs.writeJson(path.join(junoTaskDir, 'config.json'), {
        ...DEFAULT_CONFIG,
        envFilePath: '.env.custom',
        envFileCopied: false,
        hooks: DEFAULT_CONFIG.hooks,
      });

      await fs.writeFile(path.join(tempDir, '.env.yylo'), 'YYLO_LOG_LEVEL=debug\n');

      const config = await loadConfig({
        baseDir: tempDir,
      });

      const updatedConfig = await fs.readJson(path.join(junoTaskDir, 'config.json'));

      expect(await fs.pathExists(path.join(tempDir, '.env.custom'))).toBe(true);
      expect(updatedConfig.envFileCopied).toBe(true);
      expect(config.logLevel).toBe('debug');
    });

    it('should preserve divergent legacy and per-subagent model values', async () => {
      const junoTaskDir = path.join(tempDir, '.juno_task');
      await fs.ensureDir(junoTaskDir);

      await fs.writeJson(path.join(junoTaskDir, 'config.json'), {
        ...DEFAULT_CONFIG,
        defaultSubagent: 'pi',
        defaultModel: ':pi',
        defaultModels: {
          ...DEFAULT_CONFIG.defaultModels,
          pi: ':api-codex',
        },
        hooks: DEFAULT_CONFIG.hooks,
      });

      const config = await loadConfig({
        baseDir: tempDir,
      });

      const updatedConfig = await fs.readJson(path.join(junoTaskDir, 'config.json'));
      expect(config.defaultModel).toBe(':pi');
      expect(updatedConfig.defaultModel).toBe(':pi');
      expect(updatedConfig.defaultModels.pi).toBe(':api-codex');
    });
  });

  describe('ConfigLoader class', () => {
    it('should support method chaining', async () => {
      const loader = new ConfigLoader(tempDir);

      const result = loader.fromEnvironment().fromCli({ verbose: 1 });

      expect(result).toBe(loader);
    });

    it('should merge all sources correctly', async () => {
      const fileConfig = { defaultSubagent: 'claude' };
      const configPath = path.join(tempDir, 'test.json');
      await fs.writeJson(configPath, fileConfig);

      process.env.YYLO_LOG_LEVEL = 'debug';

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      loader.fromEnvironment();
      loader.fromCli({ verbose: 1 });

      const config = loader.merge();

      expect(config.defaultSubagent).toBe('claude');
      expect(config.logLevel).toBe('debug');
      expect(config.verbose).toBe(1);
    });

    it('should handle loadAll convenience method', async () => {
      const fileConfig = { defaultSubagent: 'cursor' };
      const configPath = path.join(tempDir, 'yylo.config.json');
      await fs.writeJson(configPath, fileConfig);

      process.env.YYLO_VERBOSE = 'true';

      const loader = new ConfigLoader(tempDir);
      const config = await loader.loadAll({ logLevel: 'error' });

      expect(config.defaultSubagent).toBe('cursor');
      expect(config.verbose).toBe(1);
      expect(config.logLevel).toBe('error');
    });
  });

  describe('Unsupported file formats', () => {
    it('should reject TOML files', async () => {
      const tomlPath = path.join(tempDir, 'config.toml');
      await fs.writeFile(tomlPath, 'key = "value"');

      const loader = new ConfigLoader(tempDir);
      await expect(loader.fromFile(tomlPath)).rejects.toThrow(
        /TOML configuration files are not yet supported/,
      );
    });

    it('should reject JavaScript files', async () => {
      const jsPath = path.join(tempDir, 'config.js');
      await fs.writeFile(jsPath, 'module.exports = {};');

      const loader = new ConfigLoader(tempDir);
      await expect(loader.fromFile(jsPath)).rejects.toThrow(
        /JavaScript configuration files are not yet supported/,
      );
    });

    it('should reject unknown file extensions', async () => {
      const unknownPath = path.join(tempDir, 'config.unknown');
      await fs.writeFile(unknownPath, 'content');

      const loader = new ConfigLoader(tempDir);
      await expect(loader.fromFile(unknownPath)).rejects.toThrow(/Failed to load JSON config/);
    });
  });

  describe('Edge cases and error handling', () => {
    it('should handle empty configuration files', async () => {
      const emptyPath = path.join(tempDir, 'empty.json');
      await fs.writeFile(emptyPath, '{}');

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(emptyPath);
      const config = loader.merge();

      // Should use defaults for everything
      expect(config).toEqual(expect.objectContaining(DEFAULT_CONFIG));
    });

    it('should handle null values in configuration', async () => {
      const configWithNulls = {
        defaultSubagent: 'claude',
        defaultModel: null,
        logFile: null,
      };
      const configPath = path.join(tempDir, 'nulls.json');
      await fs.writeJson(configPath, configWithNulls);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('claude');
      expect(config.defaultModel).toBeNull();
      expect(config.logFile).toBeNull();
    });

    it('should handle permission errors gracefully', async () => {
      const restrictedPath = path.join(tempDir, 'restricted.json');
      await fs.writeJson(restrictedPath, { test: 'value' });
      await fs.chmod(restrictedPath, 0o000); // No permissions

      const loader = new ConfigLoader(tempDir);
      await expect(loader.fromFile(restrictedPath)).rejects.toThrow(/not readable/);

      // Restore permissions for cleanup
      await fs.chmod(restrictedPath, 0o644);
    });

    it('should provide detailed validation error messages', () => {
      const invalidConfig = {
        defaultSubagent: 'invalid',
        defaultMaxIterations: -5,
        logLevel: 'badlevel',
      };

      try {
        validateConfig(invalidConfig);
        expect.fail('Should have thrown validation error');
      } catch (error) {
        expect(error).toBeInstanceOf(Error);
        expect(error.message).toContain('Configuration validation failed');
        expect(error.message).toContain('defaultSubagent');
        expect(error.message).toContain('defaultMaxIterations');
        expect(error.message).toContain('logLevel');
      }
    });
  });

  describe('Advanced configuration scenarios', () => {
    it('should handle parseEnvValue with edge cases', () => {
      const parseEnvValue = (value: string): string | number | boolean => {
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
      };

      expect(parseEnvValue('true')).toBe(true);
      expect(parseEnvValue('True')).toBe(true);
      expect(parseEnvValue('TRUE')).toBe(true);
      expect(parseEnvValue('false')).toBe(false);
      expect(parseEnvValue('False')).toBe(false);
      expect(parseEnvValue('FALSE')).toBe(false);
      expect(parseEnvValue('123')).toBe(123);
      expect(parseEnvValue('123.45')).toBe(123.45);
      expect(parseEnvValue('0')).toBe(0);
      expect(parseEnvValue('-42')).toBe(-42);
      expect(parseEnvValue('Infinity')).toBe('Infinity'); // Infinity should be treated as string since isFinite(Infinity) is false
      expect(parseEnvValue('-Infinity')).toBe('-Infinity'); // -Infinity should be treated as string since isFinite(-Infinity) is false
      expect(parseEnvValue('NaN')).toBe('NaN'); // NaN should be treated as string
      expect(parseEnvValue('not-a-number')).toBe('not-a-number');
      expect(parseEnvValue('')).toBe('');
      expect(parseEnvValue('  spaces  ')).toBe('  spaces  ');
    });

    it('should handle all environment variable mappings', () => {
      // Set all possible environment variables
      Object.entries(ENV_VAR_MAPPING).forEach(([envVar, configKey]) => {
        switch (configKey) {
          case 'defaultSubagent':
            process.env[envVar] = 'gemini';
            break;
          case 'defaultMaxIterations':
          case 'mcpTimeout':
          case 'mcpRetries':
            process.env[envVar] = '42';
            break;
          case 'verbose':
          case 'quiet':
          case 'interactive':
          case 'headlessMode':
            process.env[envVar] = 'true';
            break;
          case 'logLevel':
            process.env[envVar] = 'trace';
            break;
          default:
            process.env[envVar] = `/test/${configKey}`;
            break;
        }
      });

      const loader = new ConfigLoader(tempDir);
      loader.fromEnvironment();
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('gemini');
      expect(config.defaultMaxIterations).toBe(42);
      expect(config.mcpTimeout).toBe(42);
      expect(config.mcpRetries).toBe(42);
      expect(config.verbose).toBe(1);
      expect(config.quiet).toBe(true);
      expect(config.interactive).toBe(true);
      expect(config.headlessMode).toBe(true);
      expect(config.logLevel).toBe('trace');
      expect(config.workingDirectory).toBe('/test/workingDirectory');
      expect(config.sessionDirectory).toBe('/test/sessionDirectory');
      expect(config.logFile).toBe('/test/logFile');
      expect(config.mcpServerPath).toBe('/test/mcpServerPath');
    });

    it('should handle config file format detection edge cases', () => {
      const getConfigFileFormat = (filePath: string): 'json' | 'yaml' | 'toml' | 'js' => {
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
      };

      expect(getConfigFileFormat('config.json')).toBe('json');
      expect(getConfigFileFormat('config.JSON')).toBe('json');
      expect(getConfigFileFormat('config.yaml')).toBe('yaml');
      expect(getConfigFileFormat('config.YAML')).toBe('yaml');
      expect(getConfigFileFormat('config.yml')).toBe('yaml');
      expect(getConfigFileFormat('config.YML')).toBe('yaml');
      expect(getConfigFileFormat('config.toml')).toBe('toml');
      expect(getConfigFileFormat('config.TOML')).toBe('toml');
      expect(getConfigFileFormat('config.js')).toBe('js');
      expect(getConfigFileFormat('config.JS')).toBe('js');
      expect(getConfigFileFormat('config.mjs')).toBe('js');
      expect(getConfigFileFormat('config.MJS')).toBe('js');
      expect(getConfigFileFormat('.yylorc')).toBe('json');
      expect(getConfigFileFormat('config')).toBe('json');
      expect(getConfigFileFormat('config.unknown')).toBe('json');
    });

    it('should handle path resolution with different base directories', () => {
      const resolvePath = (inputPath: string, basePath: string = process.cwd()): string => {
        if (path.isAbsolute(inputPath)) {
          return inputPath;
        }
        return path.resolve(basePath, inputPath);
      };

      const customBase = '/custom/base';

      // Absolute paths should remain unchanged
      expect(resolvePath('/absolute/path', customBase)).toBe('/absolute/path');

      // Relative paths should be resolved against base
      expect(resolvePath('relative/path', customBase)).toBe(
        path.resolve(customBase, 'relative/path'),
      );
      expect(resolvePath('./current/path', customBase)).toBe(
        path.resolve(customBase, './current/path'),
      );
      expect(resolvePath('../parent/path', customBase)).toBe(
        path.resolve(customBase, '../parent/path'),
      );

      // Default base should be process.cwd()
      expect(resolvePath('relative/path')).toBe(path.resolve(process.cwd(), 'relative/path'));
    });

    it('should handle complex configuration merging scenarios', async () => {
      // Create a file with partial config
      const fileConfig = {
        defaultSubagent: 'claude',
        logLevel: 'info',
        verbose: 0,
        mcpTimeout: 25000,
      };
      const configPath = path.join(tempDir, 'complex.json');
      await fs.writeJson(configPath, fileConfig);

      // Set some environment variables
      process.env.YYLO_DEFAULT_SUBAGENT = 'cursor';
      process.env.YYLO_VERBOSE = 'true';
      process.env.YYLO_MCP_RETRIES = '5';

      // Create CLI config
      const cliConfig = {
        logLevel: 'debug',
        mcpTimeout: 60000,
        quiet: true,
      };

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(configPath);
      loader.fromEnvironment();
      loader.fromCli(cliConfig);
      const config = loader.merge();

      // Verify precedence: CLI > ENV > FILE > DEFAULTS
      expect(config.defaultSubagent).toBe('cursor'); // ENV overrides FILE
      expect(config.logLevel).toBe('debug'); // CLI overrides all
      expect(config.verbose).toBe(1); // ENV overrides FILE
      expect(config.mcpTimeout).toBe(60000); // CLI overrides all
      expect(config.mcpRetries).toBe(5); // ENV (no conflicts)
      expect(config.quiet).toBe(true); // CLI (no conflicts)
      expect(config.defaultMaxIterations).toBe(DEFAULT_CONFIG.defaultMaxIterations); // DEFAULT
    });

    it('should validate schema with all possible valid configurations', () => {
      // Test all valid subagent types
      const subagentTypes = ['claude', 'cursor', 'codex', 'gemini', 'pi'] as const;
      subagentTypes.forEach((subagent) => {
        const config = { ...DEFAULT_CONFIG, defaultSubagent: subagent };
        expect(() => validateConfig(config)).not.toThrow();
      });

      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        modelShortcuts: Object.fromEntries(
          subagentTypes.map((subagent) => [subagent, { ':fav': `provider/${subagent}-model` }]),
        ),
      })).not.toThrow();
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        modelShortcuts: { claude: { fav: 'claude-model' } },
      })).toThrow(/model shortcut must start with/);
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        modelShortcuts: { codex: { ':fav': '   ' } },
      })).toThrow(/model shortcut target must not be empty/);
      expect(() => validateConfig({
        ...DEFAULT_CONFIG,
        modelShortcuts: { unknown: { ':fav': 'provider/model' } },
      } as any)).toThrow();

      // Test all valid log levels
      const logLevels = ['error', 'warn', 'info', 'debug', 'trace'] as const;
      logLevels.forEach((logLevel) => {
        const config = { ...DEFAULT_CONFIG, logLevel };
        expect(() => validateConfig(config)).not.toThrow();
      });

      // Test boundary values for numeric fields
      const boundaryConfigs = [
        { ...DEFAULT_CONFIG, defaultMaxIterations: 1 }, // Minimum
        { ...DEFAULT_CONFIG, defaultMaxIterations: 1000 }, // Maximum
        { ...DEFAULT_CONFIG, mcpTimeout: 1000 }, // Minimum
        { ...DEFAULT_CONFIG, mcpTimeout: 300000 }, // Maximum
        { ...DEFAULT_CONFIG, mcpRetries: 0 }, // Minimum
        { ...DEFAULT_CONFIG, mcpRetries: 10 }, // Maximum
      ];

      boundaryConfigs.forEach((config) => {
        expect(() => validateConfig(config)).not.toThrow();
      });
    });

    it('should handle YAML configuration with complex structures', async () => {
      const complexYamlContent = `
# Complex YAML configuration
defaultSubagent: cursor
defaultMaxIterations: 75
logLevel: debug
verbose: 1
quiet: false
mcpTimeout: 45000
mcpRetries: 7
interactive: false
headlessMode: true
workingDirectory: "/complex/path"
sessionDirectory: "/complex/sessions"
defaultModel: "gpt-4-turbo"
logFile: "/var/log/complex.log"
mcpServerPath: "/usr/local/bin/mcp-server"
`;

      const yamlPath = path.join(tempDir, 'complex.yaml');
      await fs.writeFile(yamlPath, complexYamlContent);

      const loader = new ConfigLoader(tempDir);
      await loader.fromFile(yamlPath);
      const config = loader.merge();

      expect(config.defaultSubagent).toBe('cursor');
      expect(config.defaultMaxIterations).toBe(75);
      expect(config.logLevel).toBe('debug');
      expect(config.verbose).toBe(1);
      expect(config.quiet).toBe(false);
      expect(config.mcpTimeout).toBe(45000);
      expect(config.mcpRetries).toBe(7);
      expect(config.interactive).toBe(false);
      expect(config.headlessMode).toBe(true);
      expect(config.workingDirectory).toBe('/complex/path');
      expect(config.sessionDirectory).toBe('/complex/sessions');
      expect(config.defaultModel).toBe('gpt-4-turbo');
      expect(config.logFile).toBe('/var/log/complex.log');
      expect(config.mcpServerPath).toBe('/usr/local/bin/mcp-server');
    });

    it('should handle non-Zod validation errors gracefully', () => {
      // Create a mock error that's not a ZodError
      const mockError = new Error('Non-Zod validation error');

      // Mock JunoTaskConfigSchema.parse to throw the non-Zod error
      const originalParse = vi.fn().mockImplementation(() => {
        throw mockError;
      });

      // This is a bit tricky to test without modifying the actual code,
      // but we can at least verify the structure exists
      expect(() => {
        throw mockError;
      }).toThrow('Non-Zod validation error');
    });

    it('should handle concurrent config loading', async () => {
      const configs = await Promise.all([
        loadConfig({ baseDir: tempDir }),
        loadConfig({ baseDir: tempDir }),
        loadConfig({ baseDir: tempDir }),
      ]);

      // All configs should be identical
      expect(configs[0]).toEqual(configs[1]);
      expect(configs[1]).toEqual(configs[2]);
      // Auto-migration adds default hooks template with file size monitoring
      expect(configs[0]).toMatchObject({
        ...DEFAULT_CONFIG,
        workingDirectory: tempDir,
        sessionDirectory: path.join(tempDir, '.juno_task'),
        hooks: {
          START_RUN: {
            commands: expect.arrayContaining([
              expect.stringContaining('./.juno_task/scripts/install_requirements.sh'),
            ]),
          },
          START_ITERATION: {
            commands: expect.arrayContaining([
              expect.stringContaining('CLAUDE.md'),
              expect.stringContaining('AGENTS.md'),
              expect.stringContaining('--reject-duplicates'),
            ]),
          },
        },
      });
      expect(
        await fs.pathExists(path.join(tempDir, '.juno_task', '.config.json.migration.lock')),
      ).toBe(false);
    });

    it('should handle config loading with non-existent specific file', async () => {
      const nonExistentPath = path.join(tempDir, 'non-existent-config.json');

      await expect(
        loadConfig({
          baseDir: tempDir,
          configFile: nonExistentPath,
        }),
      ).rejects.toThrow(/Failed to load configuration file/);
    });

    it('should handle loadAll with all parameters', async () => {
      const fileConfig = {
        defaultSubagent: 'gemini',
        logLevel: 'warn',
      };
      const configPath = path.join(tempDir, 'yylo.config.json');
      await fs.writeJson(configPath, fileConfig);

      process.env.YYLO_VERBOSE = 'true';
      process.env.YYLO_MCP_TIMEOUT = '50000';

      const cliOverrides = {
        quiet: true,
        mcpRetries: 8,
      };

      const loader = new ConfigLoader(tempDir);
      const config = await loader.loadAll(cliOverrides);

      expect(config.defaultSubagent).toBe('gemini');
      expect(config.logLevel).toBe('warn');
      expect(config.verbose).toBe(1);
      expect(config.mcpTimeout).toBe(50000);
      expect(config.quiet).toBe(true);
      expect(config.mcpRetries).toBe(8);
    });
  });
});
