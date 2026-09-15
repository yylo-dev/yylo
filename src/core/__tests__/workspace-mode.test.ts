import { describe, expect, it } from 'vitest';
import { mkdtemp, mkdir, readFile, readdir, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { WorkspaceModeSchema, workspaceCapabilities, assertWorkspaceStartupSupported } from '../workspace-mode.js';
import { DEFAULT_CONFIG, JunoTaskConfigSchema, loadConfig, validateConfig } from '../config.js';

const managed = { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' };
const simple = { mode: 'simple', version: 1 };

describe('workspace mode authority', () => {
  it('preserves the existing managed shape and admits only explicit versioned Simple', () => {
    expect(WorkspaceModeSchema.parse(managed)).toEqual(managed);
    expect(WorkspaceModeSchema.parse(simple)).toEqual(simple);
    expect(validateConfig({ ...DEFAULT_CONFIG, controllerWorkspace: simple }).controllerWorkspace).toEqual(simple);
    expect(JunoTaskConfigSchema.parse({ ...DEFAULT_CONFIG, controllerWorkspace: managed }).controllerWorkspace).toEqual(managed);
  });

  it.each([
    null, {}, [], 'simple', { mode: 'simple' }, { mode: 'simple', version: 2 },
    { mode: 'simple', version: '1' }, { mode: 'unknown', version: 1 },
    { ...simple, policy: managed.policy }, { ...simple, enabled: true },
    { ...managed, version: 1 }, { mode: 'metadata-only' },
    { ...managed, policy: 'elsewhere.json' },
  ])('refuses invalid/contradictory authority %j', (value) => {
    expect(() => WorkspaceModeSchema.parse(value)).toThrow();
    expect(() => workspaceCapabilities(value)).toThrow();
    expect(() => validateConfig({ ...DEFAULT_CONFIG, controllerWorkspace: value })).toThrow();
  });

  it('does not infer Simple or managed eligibility from a missing marker', () => {
    expect(workspaceCapabilities(undefined)).toEqual(['diagnostics']);
    expect(validateConfig(DEFAULT_CONFIG).controllerWorkspace).toBeUndefined();
  });

  it('derives immutable capability eligibility without enabling managed delivery in Simple', () => {
    expect(workspaceCapabilities(simple)).toEqual(['diagnostics', 'local-agent', 'ledger', 'local-task-bookkeeping']);
    expect(workspaceCapabilities(managed)).toEqual(['diagnostics', 'ledger', 'managed-task', 'managed-merge', 'managed-integration']);
    expect(Object.isFrozen(workspaceCapabilities(simple))).toBe(true);
    expect(() => assertWorkspaceStartupSupported(simple)).not.toThrow();
    expect(() => assertWorkspaceStartupSupported(managed)).not.toThrow();
    expect(() => assertWorkspaceStartupSupported(undefined)).not.toThrow();
  });

  it('does not recommend managed migration for unrelated errors in valid Simple config', () => {
    expect(() => validateConfig({ ...DEFAULT_CONFIG, controllerWorkspace: simple, logLevel: 'bad' }))
      .toThrow(/logLevel/);
    try {
      validateConfig({ ...DEFAULT_CONFIG, controllerWorkspace: simple, logLevel: 'bad' });
    } catch (error) {
      expect(String(error)).not.toContain('prepare a metadata-only controller');
    }
  });

  it.each([false, true])('refuses unvalidated no-Git Simple startup before project writes (explicit config: %s)', async (explicit) => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'yylo-simple-config-'));
    try {
      await mkdir(path.join(root, '.juno_task'));
      const file = path.join(root, '.juno_task/config.json');
      const bytes = JSON.stringify({ controllerWorkspace: simple });
      await writeFile(file, bytes);
      await writeFile(path.join(root, 'notebook.ipynb'), 'uncommitted notebook');
      await expect(loadConfig({
        baseDir: root,
        ...(explicit ? { configFile: file } : {}),
        // CLI preferences must not erase persisted mode authority.
        cliConfig: { controllerWorkspace: managed },
      })).rejects.toThrow(/Simple MVP requires a primary Git checkout/);
      expect(await readFile(file, 'utf8')).toBe(bytes);
      expect(await readFile(path.join(root, 'notebook.ipynb'), 'utf8')).toBe('uncommitted notebook');
      expect((await readdir(root)).sort()).toEqual(['.juno_task', 'notebook.ipynb']);
      expect(await readdir(path.join(root, '.juno_task'))).toEqual(['config.json']);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
