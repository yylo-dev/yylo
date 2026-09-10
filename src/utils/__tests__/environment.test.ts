/**
 * Environment Utilities Tests
 *
 * @group unit
 */

import { describe, test, expect, beforeEach, afterEach, vi } from 'vitest';
import * as os from 'node:os';
import * as process from 'node:process';
import {
  isHeadlessEnvironment,
  isCIEnvironment,
  isInteractiveTerminal,
  isDevelopmentMode,
  getNodeEnvironment,
  getEnvVar,
  getEnvVarWithDefault,
  parseEnvBoolean,
  parseEnvNumber,
  parseEnvArray,
  getTerminalWidth,
  getTerminalHeight,
  supportsColor,
  getColorSupport,
  getPlatform,
  getArchitecture,
  getShell,
  getHomeDirectory,
  getTempDirectory,
  getConfigDirectory,
  getDataDirectory,
  getCacheDirectory,
  getProcessInfo,
  isRunningAsRoot,
  getMemoryUsage,
  getCpuUsage,
  getMCPServerEnvironment,
} from '../environment';

describe('Environment Detection', () => {
  let originalEnv: NodeJS.ProcessEnv;
  let originalStdin: any;
  let originalStdout: any;

  beforeEach(() => {
    originalEnv = { ...process.env };
    originalStdin = process.stdin;
    originalStdout = process.stdout;
  });

  afterEach(() => {
    // Restore environment variables individually
    Object.keys(process.env).forEach((key) => {
      if (!(key in originalEnv)) {
        delete process.env[key];
      } else {
        process.env[key] = originalEnv[key];
      }
    });
    // Add any missing original variables
    Object.keys(originalEnv).forEach((key) => {
      if (!(key in process.env)) {
        process.env[key] = originalEnv[key];
      }
    });
    process.stdin = originalStdin;
    process.stdout = originalStdout;
  });

  describe('isHeadlessEnvironment', () => {
    test('should return true when CI environment variable is set', () => {
      process.env.CI = 'true';
      expect(isHeadlessEnvironment()).toBe(true);
    });

    test('should return true when GITHUB_ACTIONS is set', () => {
      process.env.GITHUB_ACTIONS = 'true';
      expect(isHeadlessEnvironment()).toBe(true);
    });

    test('should return true when YYLO_HEADLESS is set', () => {
      process.env.YYLO_HEADLESS = 'true';
      expect(isHeadlessEnvironment()).toBe(true);
    });

    test('should return true when JUNO_TASK_HEADLESS is set (backward compatibility)', () => {
      process.env.JUNO_TASK_HEADLESS = 'true';
      expect(isHeadlessEnvironment()).toBe(true);
    });

    test('should return true when stdin is not a TTY', () => {
      process.stdin.isTTY = false;
      process.stdout.isTTY = true;
      expect(isHeadlessEnvironment()).toBe(true);
    });

    test('should return true when stdout is not a TTY', () => {
      process.stdin.isTTY = true;
      process.stdout.isTTY = false;
      expect(isHeadlessEnvironment()).toBe(true);
    });
  });

  describe('isCIEnvironment', () => {
    test('should return true for common CI indicators', () => {
      const ciIndicators = [
        'CI',
        'CONTINUOUS_INTEGRATION',
        'GITHUB_ACTIONS',
        'GITLAB_CI',
        'JENKINS_URL',
        'TRAVIS',
        'CIRCLECI',
      ];

      ciIndicators.forEach((indicator) => {
        // Clear environment
        Object.keys(process.env).forEach((key) => {
          if (ciIndicators.includes(key)) {
            delete process.env[key];
          }
        });

        process.env[indicator] = 'true';
        expect(isCIEnvironment()).toBe(true);
        delete process.env[indicator];
      });
    });

    test('should return false when no CI indicators are present', () => {
      const ciIndicators = [
        'CI',
        'CONTINUOUS_INTEGRATION',
        'GITHUB_ACTIONS',
        'GITLAB_CI',
        'JENKINS_URL',
        'TRAVIS',
        'CIRCLECI',
      ];

      ciIndicators.forEach((indicator) => {
        delete process.env[indicator];
      });

      expect(isCIEnvironment()).toBe(false);
    });
  });

  describe('getNodeEnvironment', () => {
    test('should return production when NODE_ENV is production', () => {
      process.env.NODE_ENV = 'production';
      expect(getNodeEnvironment()).toBe('production');
    });

    test('should return test when NODE_ENV is test', () => {
      process.env.NODE_ENV = 'test';
      expect(getNodeEnvironment()).toBe('test');
    });

    test('should return development by default', () => {
      delete process.env.NODE_ENV;
      expect(getNodeEnvironment()).toBe('development');
    });

    test('should be case insensitive', () => {
      process.env.NODE_ENV = 'PRODUCTION';
      expect(getNodeEnvironment()).toBe('production');
    });
  });

  describe('isDevelopmentMode', () => {
    test('should return true when NODE_ENV is development', () => {
      process.env.NODE_ENV = 'development';
      delete process.env.DEBUG;
      expect(isDevelopmentMode()).toBe(true);
    });

    test('should return true when DEBUG is set', () => {
      process.env.NODE_ENV = 'production';
      process.env.DEBUG = 'true';
      expect(isDevelopmentMode()).toBe(true);
    });

    test('should return false in production without DEBUG', () => {
      process.env.NODE_ENV = 'production';
      delete process.env.DEBUG;
      expect(isDevelopmentMode()).toBe(false);
    });
  });
});

describe('Environment Variables', () => {
  let originalEnv: NodeJS.ProcessEnv;

  beforeEach(() => {
    originalEnv = { ...process.env };
  });

  afterEach(() => {
    // Restore environment variables individually
    Object.keys(process.env).forEach((key) => {
      if (!(key in originalEnv)) {
        delete process.env[key];
      } else {
        process.env[key] = originalEnv[key];
      }
    });
    // Add any missing original variables
    Object.keys(originalEnv).forEach((key) => {
      if (!(key in process.env)) {
        process.env[key] = originalEnv[key];
      }
    });
  });

  describe('getEnvVar', () => {
    test('should return environment variable value', () => {
      process.env.TEST_VAR = 'test_value';
      expect(getEnvVar('TEST_VAR')).toBe('test_value');
    });

    test('should return default value when variable is not set', () => {
      delete process.env.TEST_VAR;
      expect(getEnvVar('TEST_VAR', 'default')).toBe('default');
    });

    test('should return undefined when variable is not set and no default', () => {
      delete process.env.TEST_VAR;
      expect(getEnvVar('TEST_VAR')).toBeUndefined();
    });
  });

  describe('getEnvVarWithDefault', () => {
    test('should return environment variable value', () => {
      process.env.TEST_VAR = 'test_value';
      expect(getEnvVarWithDefault('TEST_VAR', 'default')).toBe('test_value');
    });

    test('should return default value when variable is not set', () => {
      delete process.env.TEST_VAR;
      expect(getEnvVarWithDefault('TEST_VAR', 'default')).toBe('default');
    });
  });

  describe('parseEnvBoolean', () => {
    test('should parse true values correctly', () => {
      expect(parseEnvBoolean('true')).toBe(true);
      expect(parseEnvBoolean('TRUE')).toBe(true);
      expect(parseEnvBoolean('1')).toBe(true);
      expect(parseEnvBoolean('yes')).toBe(true);
      expect(parseEnvBoolean('YES')).toBe(true);
      expect(parseEnvBoolean('on')).toBe(true);
      expect(parseEnvBoolean('enabled')).toBe(true);
    });

    test('should parse false values correctly', () => {
      expect(parseEnvBoolean('false')).toBe(false);
      expect(parseEnvBoolean('0')).toBe(false);
      expect(parseEnvBoolean('no')).toBe(false);
      expect(parseEnvBoolean('off')).toBe(false);
      expect(parseEnvBoolean('disabled')).toBe(false);
    });

    test('should return default value for undefined', () => {
      expect(parseEnvBoolean(undefined)).toBe(false);
      expect(parseEnvBoolean(undefined, true)).toBe(true);
    });

    test('should handle whitespace', () => {
      expect(parseEnvBoolean('  true  ')).toBe(true);
      expect(parseEnvBoolean('  false  ')).toBe(false);
    });
  });

  describe('parseEnvNumber', () => {
    test('should parse valid numbers', () => {
      expect(parseEnvNumber('42')).toBe(42);
      expect(parseEnvNumber('3.14')).toBe(3.14);
      expect(parseEnvNumber('-10')).toBe(-10);
      expect(parseEnvNumber('0')).toBe(0);
    });

    test('should return default for invalid numbers', () => {
      expect(parseEnvNumber('not_a_number')).toBe(0);
      expect(parseEnvNumber('not_a_number', 100)).toBe(100);
    });

    test('should return default for undefined', () => {
      expect(parseEnvNumber(undefined)).toBe(0);
      expect(parseEnvNumber(undefined, 42)).toBe(42);
    });
  });

  describe('parseEnvArray', () => {
    test('should parse comma-separated values', () => {
      expect(parseEnvArray('a,b,c')).toEqual(['a', 'b', 'c']);
    });

    test('should handle custom delimiter', () => {
      expect(parseEnvArray('a:b:c', ':')).toEqual(['a', 'b', 'c']);
    });

    test('should trim whitespace', () => {
      expect(parseEnvArray('a, b , c')).toEqual(['a', 'b', 'c']);
    });

    test('should filter empty values', () => {
      expect(parseEnvArray('a,,b,,,c')).toEqual(['a', 'b', 'c']);
    });

    test('should return default for undefined', () => {
      expect(parseEnvArray(undefined)).toEqual([]);
      expect(parseEnvArray(undefined, ',', ['default'])).toEqual(['default']);
    });
  });
});

describe('Terminal Capabilities', () => {
  let originalStdout: any;

  beforeEach(() => {
    originalStdout = process.stdout;
  });

  afterEach(() => {
    process.stdout = originalStdout;
  });

  describe('getTerminalWidth', () => {
    test('should return stdout columns', () => {
      process.stdout.columns = 120;
      expect(getTerminalWidth()).toBe(120);
    });

    test('should return default when columns is undefined', () => {
      process.stdout.columns = undefined;
      expect(getTerminalWidth()).toBe(80);
    });
  });

  describe('getTerminalHeight', () => {
    test('should return stdout rows', () => {
      process.stdout.rows = 30;
      expect(getTerminalHeight()).toBe(30);
    });

    test('should return default when rows is undefined', () => {
      process.stdout.rows = undefined;
      expect(getTerminalHeight()).toBe(24);
    });
  });

  describe('supportsColor', () => {
    let originalEnv: NodeJS.ProcessEnv;

    beforeEach(() => {
      originalEnv = { ...process.env };
    });

    afterEach(() => {
      // Restore environment variables individually
      Object.keys(process.env).forEach((key) => {
        if (!(key in originalEnv)) {
          delete process.env[key];
        } else {
          process.env[key] = originalEnv[key];
        }
      });
      // Add any missing original variables
      Object.keys(originalEnv).forEach((key) => {
        if (!(key in process.env)) {
          process.env[key] = originalEnv[key];
        }
      });
    });

    test('should return true when FORCE_COLOR is set', () => {
      process.env.FORCE_COLOR = '1';
      expect(supportsColor()).toBe(true);
    });

    test('should return false when NO_COLOR is set', () => {
      delete process.env.FORCE_COLOR;
      process.env.NO_COLOR = '1';
      expect(supportsColor()).toBe(false);
    });

    test('should return false when NODE_DISABLE_COLORS is set', () => {
      delete process.env.FORCE_COLOR;
      process.env.NODE_DISABLE_COLORS = '1';
      expect(supportsColor()).toBe(false);
    });

    test('should return false when TERM is dumb', () => {
      delete process.env.FORCE_COLOR;
      delete process.env.NO_COLOR;
      process.env.TERM = 'dumb';
      process.stdout.isTTY = true;
      expect(supportsColor()).toBe(false);
    });
  });

  describe('getColorSupport', () => {
    let originalEnv: NodeJS.ProcessEnv;

    beforeEach(() => {
      originalEnv = { ...process.env };
    });

    afterEach(() => {
      // Restore environment variables individually
      Object.keys(process.env).forEach((key) => {
        if (!(key in originalEnv)) {
          delete process.env[key];
        } else {
          process.env[key] = originalEnv[key];
        }
      });
      // Add any missing original variables
      Object.keys(originalEnv).forEach((key) => {
        if (!(key in process.env)) {
          process.env[key] = originalEnv[key];
        }
      });
    });

    test('should return none when colors are not supported', () => {
      delete process.env.FORCE_COLOR;
      process.env.NO_COLOR = '1';
      expect(getColorSupport()).toBe('none');
    });

    test('should return truecolor when COLORTERM is truecolor', () => {
      delete process.env.NO_COLOR;
      process.env.FORCE_COLOR = '1';
      process.env.COLORTERM = 'truecolor';
      expect(getColorSupport()).toBe('truecolor');
    });

    test('should return 256 when TERM includes 256', () => {
      delete process.env.NO_COLOR;
      process.env.FORCE_COLOR = '1';
      delete process.env.COLORTERM;
      process.env.TERM = 'xterm-256color';
      expect(getColorSupport()).toBe('256');
    });

    test('should return basic by default', () => {
      delete process.env.NO_COLOR;
      process.env.FORCE_COLOR = '1';
      delete process.env.COLORTERM;
      process.env.TERM = 'xterm';
      expect(getColorSupport()).toBe('basic');
    });
  });
});

describe('Platform Information', () => {
  test('getPlatform should return os.platform result', () => {
    expect(getPlatform()).toBe(os.platform());
  });

  test('getArchitecture should return os.arch result', () => {
    expect(getArchitecture()).toBe(os.arch());
  });

  test('getHomeDirectory should return os.homedir result', () => {
    expect(getHomeDirectory()).toBe(os.homedir());
  });

  test('getTempDirectory should return os.tmpdir result', () => {
    expect(getTempDirectory()).toBe(os.tmpdir());
  });

  describe('getShell', () => {
    let originalEnv: NodeJS.ProcessEnv;

    beforeEach(() => {
      originalEnv = { ...process.env };
    });

    afterEach(() => {
      // Restore environment variables individually
      Object.keys(process.env).forEach((key) => {
        if (!(key in originalEnv)) {
          delete process.env[key];
        } else {
          process.env[key] = originalEnv[key];
        }
      });
      // Add any missing original variables
      Object.keys(originalEnv).forEach((key) => {
        if (!(key in process.env)) {
          process.env[key] = originalEnv[key];
        }
      });
    });

    test('should detect bash', () => {
      process.env.SHELL = '/bin/bash';
      expect(getShell()).toBe('bash');
    });

    test('should detect zsh', () => {
      process.env.SHELL = '/usr/local/bin/zsh';
      expect(getShell()).toBe('zsh');
    });

    test('should detect fish', () => {
      process.env.SHELL = '/usr/local/bin/fish';
      expect(getShell()).toBe('fish');
    });

    test('should detect cmd on Windows', () => {
      delete process.env.SHELL;
      process.env.ComSpec = 'C:\\Windows\\System32\\cmd.exe';
      expect(getShell()).toBe('cmd');
    });

    test('should return unknown for unrecognized shells', () => {
      process.env.SHELL = '/usr/local/bin/unknown-shell';
      expect(getShell()).toBe('unknown');
    });
  });
});

describe('Configuration Directories', () => {
  test('getConfigDirectory should return platform-specific paths', () => {
    const configDir = getConfigDirectory('test-app');
    expect(configDir).toContain('test-app');
    expect(typeof configDir).toBe('string');
  });

  test('getDataDirectory should return platform-specific paths', () => {
    const dataDir = getDataDirectory('test-app');
    expect(dataDir).toContain('test-app');
    expect(typeof dataDir).toBe('string');
  });

  test('getCacheDirectory should return platform-specific paths', () => {
    const cacheDir = getCacheDirectory('test-app');
    expect(cacheDir).toContain('test-app');
    expect(typeof cacheDir).toBe('string');
  });
});

describe('Process Information', () => {
  test('getProcessInfo should return process information', () => {
    const info = getProcessInfo();
    expect(info.pid).toBe(process.pid);
    expect(info.platform).toBe(os.platform());
    expect(info.arch).toBe(os.arch());
    expect(info.nodeVersion).toBe(process.version);
    expect(typeof info.uptime).toBe('number');
    expect(typeof info.cwd).toBe('string');
    expect(typeof info.execPath).toBe('string');
    expect(Array.isArray(info.argv)).toBe(true);
    expect(typeof info.env).toBe('object');
  });

  test('getMemoryUsage should return memory usage', () => {
    const memory = getMemoryUsage();
    expect(typeof memory.rss).toBe('number');
    expect(typeof memory.heapTotal).toBe('number');
    expect(typeof memory.heapUsed).toBe('number');
    expect(typeof memory.external).toBe('number');
    expect(typeof memory.arrayBuffers).toBe('number');
  });

  test('getCpuUsage should return CPU usage', () => {
    const cpu = getCpuUsage();
    expect(typeof cpu.user).toBe('number');
    expect(typeof cpu.system).toBe('number');
  });
});

describe('MCP Server Environment', () => {
  test('getMCPServerEnvironment should return environment configuration', () => {
    const env = getMCPServerEnvironment({ DEBUG: 'true' });
    expect(env.PATH).toBe(process.env.PATH || '');
    expect(env.NODE_ENV).toBe(getNodeEnvironment());
    expect(env.HOME).toBe(getHomeDirectory());
    expect(env.TMPDIR).toBe(getTempDirectory());
    expect(env.DEBUG).toBe('true');
  });

  test('getMCPServerEnvironment should handle additional environment variables', () => {
    const env = getMCPServerEnvironment({
      CUSTOM_VAR: 'custom_value',
      ANOTHER_VAR: 'another_value',
    });
    expect(env.CUSTOM_VAR).toBe('custom_value');
    expect(env.ANOTHER_VAR).toBe('another_value');
  });
});
