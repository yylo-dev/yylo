import { describe, expect, it, vi } from 'vitest';
import {
  invokeMachineAwareChild,
  MACHINE_RESPONSE_SCHEMA,
  resolveMachineOutput,
  writeMachineResponse,
} from '../machine-output.js';

function capture(stream: NodeJS.WriteStream, run: () => void): string {
  let output = '';
  const spy = vi.spyOn(stream, 'write').mockImplementation(((chunk: string | Uint8Array) => {
    output += chunk.toString();
    return true;
  }) as typeof stream.write);
  try { run(); } finally { spy.mockRestore(); }
  return output;
}

describe('strict machine output', () => {
  it('distinguishes Ledger -f from the root prompt-file option', () => {
    expect(resolveMachineOutput(['-f', 'json'])).toBeUndefined();
    expect(resolveMachineOutput(['ledger', 'get', 'T1', '-f', 'json', '--raw']))
      .toMatchObject({ format: 'json', raw: true });
    expect(resolveMachineOutput(['task', 'status', 'T1', '--format=ndjson']))
      .toMatchObject({ format: 'ndjson' });
  });

  it('writes compact JSON and one-line NDJSON envelopes', () => {
    const response = {
      schema_version: MACHINE_RESPONSE_SCHEMA,
      command: { name: 'task.status', version: 1 as const },
      status: 'success' as const,
      projection: 'default', data: { path: 'a b\ncontrol' }, error: null,
    };
    const json = capture(process.stdout, () => writeMachineResponse(response, {
      format: 'json', raw: true, projection: 'default',
    }));
    expect(JSON.parse(json)).toEqual(response);
    const ndjson = capture(process.stdout, () => writeMachineResponse(response, {
      format: 'ndjson', raw: false, projection: 'default',
    }));
    expect(ndjson.trim().split('\n')).toHaveLength(1);
    expect(JSON.parse(ndjson)).toEqual(response);
  });

  it('keeps diagnostics on stderr and emits a typed refusal with nonzero status', async () => {
    let stdout = '';
    let stderr = '';
    const stdoutSpy = vi.spyOn(process.stdout, 'write').mockImplementation(((chunk: string | Uint8Array) => {
      stdout += chunk.toString(); return true;
    }) as typeof process.stdout.write);
    const stderrSpy = vi.spyOn(process.stderr, 'write').mockImplementation(((chunk: string | Uint8Array) => {
      stderr += chunk.toString(); return true;
    }) as typeof process.stderr.write);
    try {
      const result = await invokeMachineAwareChild({
        executable: process.execPath,
        args: ['-e', "process.stderr.write('refused path with spaces\\n');process.exit(2)"],
        cwd: process.cwd(), env: process.env, command: 'task.finish',
        machine: { format: 'json', raw: true, projection: 'default' },
      });
      expect(result.exitCode).toBe(2);
    } finally {
      stdoutSpy.mockRestore(); stderrSpy.mockRestore();
    }
    expect(stderr).toContain('refused path with spaces');
    expect(JSON.parse(stdout)).toMatchObject({
      schema_version: MACHINE_RESPONSE_SCHEMA,
      command: { name: 'task.finish', version: 1 },
      status: 'refusal', data: null,
      error: { code: 'COMMAND_REFUSED', exit_code: 2 },
    });
  });

  it('fails closed when nested output contaminates JSON', async () => {
    let stdout = '';
    const stdoutSpy = vi.spyOn(process.stdout, 'write').mockImplementation(((chunk: string | Uint8Array) => {
      stdout += chunk.toString(); return true;
    }) as typeof process.stdout.write);
    const stderrSpy = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    try {
      const result = await invokeMachineAwareChild({
        executable: process.execPath,
        args: ['-e', "process.stdout.write('banner\\n{\\\"ok\\\":true}\\n')"],
        cwd: process.cwd(), env: process.env, command: 'integration.status',
        machine: { format: 'json', raw: true, projection: 'default' },
      });
      expect(result.exitCode).toBe(70);
    } finally {
      stdoutSpy.mockRestore(); stderrSpy.mockRestore();
    }
    expect(JSON.parse(stdout)).toMatchObject({ status: 'error', error: { code: 'INVALID_CHILD_PAYLOAD' } });
  });
});
