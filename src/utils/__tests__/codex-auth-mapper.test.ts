import { describe, it, expect } from 'vitest';
import fs from 'fs-extra';
import path from 'node:path';
import os from 'node:os';

import { importCodexAuth } from '../codex-auth-mapper.js';

function createJwt(payload: Record<string, unknown>): string {
  const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  return `${header}.${body}.signature`;
}

describe('codex-auth-mapper', () => {
  it('imports codex auth into pi auth.json and preserves existing providers', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'codex-auth-map-'));
    const inputPath = path.join(tempDir, 'codex-auth.json');
    const outputPath = path.join(tempDir, 'pi-auth.json');

    const expSeconds = Math.floor(Date.now() / 1000) + 3600;
    const accessToken = createJwt({
      exp: expSeconds,
      'https://api.openai.com/auth': { chatgpt_account_id: 'acc_123' },
    });

    await fs.writeJson(
      inputPath,
      {
        auth_mode: 'chatgpt',
        tokens: {
          id_token: 'idtok',
          access_token: accessToken,
          refresh_token: 'reftok',
          account_id: 'acc_123',
        },
        last_refresh: '2026-03-08T00:00:00.000Z',
      },
      { spaces: 2 },
    );

    await fs.writeJson(
      outputPath,
      {
        anthropic: {
          type: 'api_key',
          key: 'sk-ant-test',
        },
      },
      { spaces: 2 },
    );

    const result = await importCodexAuth({ inputPath, outputPath });

    const output = await fs.readJson(outputPath);
    expect(result.provider).toBe('openai-codex');
    expect(output.anthropic).toEqual({ type: 'api_key', key: 'sk-ant-test' });
    expect(output['openai-codex']).toMatchObject({
      type: 'oauth',
      access: accessToken,
      refresh: 'reftok',
      accountId: 'acc_123',
    });
    expect(output['openai-codex'].expires).toBe(expSeconds * 1000);
  });

  it('supports custom provider id and falls back to immediate refresh when token has no exp', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'codex-auth-map-'));
    const inputPath = path.join(tempDir, 'codex-auth.json');
    const outputPath = path.join(tempDir, 'pi-auth.json');

    await fs.writeJson(
      inputPath,
      {
        auth_mode: 'chatgpt',
        tokens: {
          id_token: 'nojwt',
          access_token: 'nojwt2',
          refresh_token: 'reftok',
          account_id: 'acc_456',
        },
      },
      { spaces: 2 },
    );

    const before = Date.now();
    await importCodexAuth({
      inputPath,
      outputPath,
      provider: 'openai-codex-alt',
    });
    const after = Date.now();

    const output = await fs.readJson(outputPath);
    expect(output['openai-codex-alt'].type).toBe('oauth');
    expect(output['openai-codex-alt'].expires).toBeGreaterThanOrEqual(before - 1000);
    expect(output['openai-codex-alt'].expires).toBeLessThanOrEqual(after);
  });

  it('throws a helpful error when codex auth file is missing required token fields', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'codex-auth-map-'));
    const inputPath = path.join(tempDir, 'codex-auth.json');
    const outputPath = path.join(tempDir, 'pi-auth.json');

    await fs.writeJson(
      inputPath,
      {
        tokens: {
          access_token: 'x',
        },
      },
      { spaces: 2 },
    );

    await expect(importCodexAuth({ inputPath, outputPath })).rejects.toThrow(
      /refresh_token/i,
    );
  });
});
