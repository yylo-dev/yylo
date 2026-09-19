import { spawn } from 'node:child_process';
import stripAnsi from 'strip-ansi';
import type { Command } from 'commander';

export const MACHINE_RESPONSE_SCHEMA = 'yylo.machine-response.v1' as const;
export type MachineFormat = 'json' | 'ndjson';

export interface MachineOutputRequest {
  format: MachineFormat;
  raw: boolean;
  projection: string;
}

export interface MachineResponse {
  schema_version: typeof MACHINE_RESPONSE_SCHEMA;
  command: { name: string; version: 1 };
  status: 'success' | 'refusal' | 'error';
  projection: string;
  data: unknown | null;
  error: { code: string; message: string; exit_code: number } | null;
}

const MAX_CAPTURE_BYTES = 1024 * 1024;
const REFUSAL_EXIT_CODES = new Set([2, 64, 65, 69, 77, 78, 99]);

function optionValue(args: readonly string[], names: readonly string[]): string | undefined {
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index]!;
    for (const name of names) {
      if (token === name) return args[index + 1];
      if (token.startsWith(`${name}=`)) return token.slice(name.length + 1);
    }
  }
  return undefined;
}

/** Resolve only explicitly advertised machine modes; ordinary human output is untouched. */
export function resolveMachineOutput(
  args: readonly string[],
  options: { jsonFlag?: boolean; defaultProjection?: string } = {},
): MachineOutputRequest | undefined {
  const longFormat = optionValue(args, ['--format']);
  // Root -f means prompt-file. The short format spelling is therefore machine
  // mode only when paired with Ledger's documented --raw discriminator.
  const shortFormat = args.includes('--raw') ? optionValue(args, ['-f']) : undefined;
  const flagFormat = args.includes('--ndjson') ? 'ndjson' : options.jsonFlag && args.includes('--json') ? 'json' : undefined;
  const format = longFormat ?? shortFormat ?? flagFormat;
  if (format === undefined) return undefined;
  if (format !== 'json' && format !== 'ndjson') return undefined;
  return {
    format,
    raw: args.includes('--raw'),
    projection: options.defaultProjection ?? 'default',
  };
}

/** Remove facade-owned framing options before invoking a runtime that predates this contract. */
export function stripMachineOutputArgs(args: readonly string[]): string[] {
  const result: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index]!;
    if (['--raw', '--ndjson'].includes(token)) continue;
    if (['--format', '-f'].includes(token)) {
      index += 1;
      continue;
    }
    if (['--format=', '-f='].some((prefix) => token.startsWith(prefix))) continue;
    result.push(token);
  }
  return result;
}

export function addMachineOutputOptions(command: Command): Command {
  return command
    .option('--format <format>', 'Machine response format: json or ndjson')
    .option('--raw', 'Emit compact machine output (machine mode only)');
}

function parsePayload(stdout: string, format: MachineFormat): unknown | null {
  if (!stdout.trim()) return null;
  if (format === 'json') return JSON.parse(stdout) as unknown;
  // Managed lifecycle runtimes historically print pretty JSON even when the
  // facade selects NDJSON. Accept that one document, otherwise parse the
  // independently delegated Ledger stream one record per line.
  try { return JSON.parse(stdout) as unknown; }
  catch {
    return stdout.split(/\r?\n/).filter((line) => line.trim()).map((line) => JSON.parse(line) as unknown);
  }
}

function diagnosticMessage(stderr: string, fallback: string): string {
  const lines = stripAnsi(stderr).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  return (lines.at(-1) ?? fallback).slice(0, 4096);
}

export function currentMachineCommand(args: readonly string[]): string {
  const surfaceIndex = args.findIndex((token) => ['task', 'merge', 'integration', 'ledger', 'kanban', 'wiki'].includes(token));
  if (surfaceIndex < 0) return 'yylo';
  if (args[surfaceIndex] === 'wiki') return 'ledger.wiki';
  const surface = args[surfaceIndex] === 'kanban' ? 'ledger' : args[surfaceIndex]!;
  const operation = args.slice(surfaceIndex + 1).find((token) => !token.startsWith('-'));
  return operation ? `${surface}.${operation}` : surface;
}

export function writeCurrentMachineError(error: unknown, exitCode: number): boolean {
  const request = resolveMachineOutput(process.argv.slice(2), { jsonFlag: true });
  if (!request) return false;
  const command = currentMachineCommand(process.argv.slice(2));
  writeMachineResponse({
    schema_version: MACHINE_RESPONSE_SCHEMA,
    command: { name: command, version: 1 },
    status: REFUSAL_EXIT_CODES.has(exitCode) ? 'refusal' : 'error',
    projection: request.projection,
    data: null,
    error: {
      code: REFUSAL_EXIT_CODES.has(exitCode) ? 'COMMAND_REFUSED' : 'COMMAND_FAILED',
      message: (error instanceof Error ? error.message : String(error)).slice(0, 4096),
      exit_code: exitCode,
    },
  }, request);
  return true;
}

export function writeMachineResponse(response: MachineResponse, request: MachineOutputRequest): void {
  const spacing = request.raw ? undefined : 2;
  // NDJSON is one bounded response object per line. Pretty printing is never valid NDJSON.
  process.stdout.write(`${JSON.stringify(response, null, request.format === 'ndjson' ? undefined : spacing)}\n`);
}

export async function invokeMachineAwareChild(options: {
  executable: string;
  args: readonly string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  command: string;
  machine?: MachineOutputRequest;
}): Promise<{ exitCode: number; payload: unknown | null }> {
  if (!options.machine) {
    const exitCode = await new Promise<number>((resolve, reject) => {
      const child = spawn(options.executable, [...options.args], {
        cwd: options.cwd, env: options.env, stdio: 'inherit',
      });
      child.once('error', reject);
      child.once('exit', (code, signal) => signal
        ? reject(new Error(`${options.command} terminated by signal ${signal}`))
        : resolve(code ?? 1));
    });
    return { exitCode, payload: null };
  }

  let stdout = Buffer.alloc(0);
  let stderr = Buffer.alloc(0);
  let overflow = false;
  const exitCode = await new Promise<number>((resolve) => {
    const child = spawn(options.executable, [...options.args], {
      cwd: options.cwd, env: options.env, stdio: ['inherit', 'pipe', 'pipe'],
    });
    child.stdout.on('data', (chunk: Buffer) => {
      if (stdout.length + chunk.length <= MAX_CAPTURE_BYTES) stdout = Buffer.concat([stdout, chunk]);
      else overflow = true;
    });
    child.stderr.on('data', (chunk: Buffer) => {
      process.stderr.write(chunk);
      if (stderr.length < MAX_CAPTURE_BYTES) stderr = Buffer.concat([stderr, chunk.subarray(0, MAX_CAPTURE_BYTES - stderr.length)]);
    });
    child.once('error', (error) => {
      const text = `${options.command}: ${error.message}\n`;
      process.stderr.write(text);
      stderr = Buffer.from(text);
      resolve(126);
    });
    child.once('close', (code, signal) => {
      if (signal) {
        const text = `${options.command} terminated by signal ${signal}\n`;
        process.stderr.write(text);
        stderr = Buffer.concat([stderr, Buffer.from(text)]);
        resolve(128);
      } else resolve(code ?? 1);
    });
  });

  let payload: unknown | null = null;
  let framingError: string | undefined;
  if (overflow) framingError = `child stdout exceeded ${MAX_CAPTURE_BYTES} bytes`;
  else {
    try { payload = parsePayload(stdout.toString('utf8'), options.machine.format); }
    catch (error) { framingError = `child emitted invalid ${options.machine.format}: ${error instanceof Error ? error.message : String(error)}`; }
  }
  const effectiveExit = framingError ? 70 : exitCode;
  if (framingError) process.stderr.write(`${options.command}: ${framingError}\n`);
  const status = effectiveExit === 0 ? 'success' : REFUSAL_EXIT_CODES.has(effectiveExit) ? 'refusal' : 'error';
  writeMachineResponse({
    schema_version: MACHINE_RESPONSE_SCHEMA,
    command: { name: options.command, version: 1 },
    status,
    projection: options.machine.projection,
    data: payload,
    error: effectiveExit === 0 ? null : {
      code: framingError ? 'INVALID_CHILD_PAYLOAD' : status === 'refusal' ? 'COMMAND_REFUSED' : 'COMMAND_FAILED',
      message: framingError ?? diagnosticMessage(stderr.toString('utf8'), `${options.command} exited ${effectiveExit}`),
      exit_code: effectiveExit,
    },
  }, options.machine);
  return { exitCode: effectiveExit, payload };
}
