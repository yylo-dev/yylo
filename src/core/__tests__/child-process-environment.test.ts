import { describe, expect, it } from 'vitest';

import {
  buildChildProcessEnvironment,
  isContinuityEnvironmentKey,
} from '../child-process-environment.js';

describe('child process environment boundary', () => {
  it('recognizes legacy keys and the complete reserved scoped namespaces', () => {
    expect(isContinuityEnvironmentKey('YYLO_LAST_SESSION_ID')).toBe(true);
    expect(isContinuityEnvironmentKey('YYLO_LAST_EXECUTION_SETTINGS')).toBe(true);
    expect(isContinuityEnvironmentKey('YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF')).toBe(true);
    expect(isContinuityEnvironmentKey('YYLO_LAST_EXECUTION_SETTINGS_SCOPE_0123456789ABCDEF')).toBe(true);
    expect(isContinuityEnvironmentKey('YYLO_LAST_SESSION_ID_SCOPE_lowercase')).toBe(true);
    expect(isContinuityEnvironmentKey('YYLO_LAST_EXECUTION_SETTINGS_SCOPE_')).toBe(true);
    expect(isContinuityEnvironmentKey('YYLO_CONTINUE_SCOPE')).toBe(false);
    expect(isContinuityEnvironmentKey('YYLO_LAST_SESSION_ID_BACKUP')).toBe(false);
  });

  it('preserves arbitrary config and routing while filtering continuity from base and overrides', () => {
    const environment = buildChildProcessEnvironment(
      {
        API_TOKEN: 'sample',
        CUSTOM_CONFIG: 'kept',
        JUNO_TASK_ROOT: '/controller',
        JUNO_CONTROLLER_BRANCH: 'main',
        JUNO_WORKSPACE_ROLE: 'task',
        JUNO_WORKSPACE_ENFORCEMENT: 'strict',
        YYLO_CONTINUE_SCOPE: 'pinned-scope',
        YYLO_LAST_SESSION_ID: 'legacy',
        YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF: 'historical',
        YYLO_LAST_SESSION_ID_SCOPE_malformed_old_suffix: 'historical-malformed',
      },
      {
        DISPATCH_ONLY: 'current',
        YYLO_LAST_EXECUTION_SETTINGS_SCOPE_FEDCBA9876543210: 'must-not-return',
      },
    );

    expect(environment).toEqual({
      API_TOKEN: 'sample',
      CUSTOM_CONFIG: 'kept',
      JUNO_TASK_ROOT: '/controller',
      JUNO_CONTROLLER_BRANCH: 'main',
      JUNO_WORKSPACE_ROLE: 'task',
      JUNO_WORKSPACE_ENFORCEMENT: 'strict',
      YYLO_CONTINUE_SCOPE: 'pinned-scope',
      DISPATCH_ONLY: 'current',
    });
  });

  it('has O(1) continuity overhead with 2,500 historical pairs', () => {
    const stale: NodeJS.ProcessEnv = {};
    for (let index = 0; index < 2_500; index += 1) {
      const scope = `SCOPE_${index.toString(16).toUpperCase().padStart(16, '0')}`;
      stale[`YYLO_LAST_SESSION_ID_${scope}`] = `session-${index}`;
      stale[`YYLO_LAST_EXECUTION_SETTINGS_${scope}`] = `settings-${index}`;
    }
    stale.PROVIDER_API_KEY = 'masked';
    stale.JUNO_TASK_ROOT = '/controller';

    const filtered = buildChildProcessEnvironment(stale, { JUNO_MODEL: 'model' });
    const names = Object.keys(filtered).sort();
    const continuityNames = names.filter(isContinuityEnvironmentKey);
    const serializedBytes = Buffer.byteLength(
      names.map((name) => `${name}=${filtered[name] ?? ''}\0`).join(''),
      'utf8',
    );

    expect(Object.keys(stale)).toHaveLength(5_002);
    expect(continuityNames).toEqual([]);
    expect(names).toEqual(['JUNO_MODEL', 'JUNO_TASK_ROOT', 'PROVIDER_API_KEY']);
    expect(serializedBytes).toBe(68);
  });
});
