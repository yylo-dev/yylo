import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { constants as osConstants } from 'node:os';
import type { Command } from 'commander';
import packageMetadata from '../../../package.json';
import { hasSimpleWorkspaceHint, resolveController } from '../../utils/controller-resolver.js';
import { buildChildProcessEnvironment } from '../../core/child-process-environment.js';
import { markTransparentDelegate } from '../../utils/explicit-command.js';

// This is intentionally exact and sourced from release metadata: a YYLO
// release is validated with one independently packaged ledger artifact.
export const LEDGER_VERSION_RANGE = packageMetadata.yyloLedger.version;
const VERSION_HANDSHAKE_TIMEOUT_MS = 10_000;
const SIGNALS = ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT'] as const;
type ForwardedSignal = (typeof SIGNALS)[number];

let activeChild: ChildProcess | undefined;

export class LedgerDelegateError extends Error {
  constructor(message: string, readonly exitCode: number) {
    super(message);
    this.name = 'LedgerDelegateError';
  }
}

export interface LedgerDelegateResult {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
}

function executableCandidates(
  name: string,
  env: NodeJS.ProcessEnv,
  cwd: string,
): string[] {
  const pathValue = env.PATH ?? '';
  const extensions = process.platform === 'win32'
    ? (env.PATHEXT ?? '.EXE;.CMD;.BAT;.COM').split(';')
    : [''];
  return pathValue.split(path.delimiter).flatMap((entry) => {
    // Empty and relative PATH entries are interpreted from the delegated cwd,
    // matching executable lookup after spawn changes into that directory.
    const directory = path.resolve(cwd, entry || '.');
    return extensions.map((extension) => path.join(directory, `${name}${extension}`));
  });
}

export function discoverLedgerExecutable(
  env: NodeJS.ProcessEnv = process.env,
  name = 'yylo-ledger',
  cwd = process.cwd(),
): string {
  for (const candidate of executableCandidates(name, env, cwd)) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      // Continue through PATH. Discovery deliberately has no repository-local fallback.
    }
  }
  throw new LedgerDelegateError(
    `yylo: cannot find independently installed '${name}' on PATH (cwd: ${cwd}). ` +
      `Install a compatible yylo-ledger (${LEDGER_VERSION_RANGE}) and retry.`,
    127,
  );
}

function waitForChild(
  child: ChildProcess,
  timeout?: { readonly milliseconds: number; readonly description: string },
): Promise<LedgerDelegateResult> {
  activeChild = child;
  return new Promise<LedgerDelegateResult>((resolve, reject) => {
    let timedOut = false;
    const timer = timeout === undefined ? undefined : setTimeout(() => {
      timedOut = true;
      if (!child.kill('SIGKILL')) {
        reject(new LedgerDelegateError(
          `yylo: ${timeout.description} timed out after ${timeout.milliseconds}ms.`,
          69,
        ));
      }
    }, timeout.milliseconds);
    timer?.unref();
    child.once('error', (error) => {
      if (timer !== undefined) clearTimeout(timer);
      reject(error);
    });
    child.once('exit', (code, signal) => {
      if (timer !== undefined) clearTimeout(timer);
      if (timedOut && timeout !== undefined) {
        reject(new LedgerDelegateError(
          `yylo: ${timeout.description} timed out after ${timeout.milliseconds}ms.`,
          69,
        ));
      } else {
        resolve({ code, signal });
      }
    });
  }).finally(() => {
    if (activeChild === child) activeChild = undefined;
  });
}

/** Called by the CLI's process-level signal handlers while delegation is active. */
export function forwardLedgerSignal(signal: ForwardedSignal): boolean {
  if (activeChild === undefined || activeChild.exitCode !== null || activeChild.signalCode !== null) {
    return false;
  }
  activeChild.kill(signal);
  return true;
}

function parseVersion(output: string): string | undefined {
  const match = output.trim().match(/^(?:yylo-ledger\s+)?(\d+\.\d+\.\d+(?:rc\d+)?)$/);
  if (match === null) return undefined;
  return match[1];
}

function isCompatibleVersion(version: string): boolean {
  return version === LEDGER_VERSION_RANGE;
}

async function readVersion(
  executable: string,
  cwd: string,
  env: NodeJS.ProcessEnv,
  timeoutMs: number,
): Promise<string> {
  const child = spawn(executable, ['--version'], {
    cwd,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  let bytes = 0;
  const collect = (chunks: Buffer[], chunk: Buffer) => {
    bytes += chunk.length;
    if (bytes > 64 * 1024) child.kill('SIGKILL');
    else chunks.push(chunk);
  };
  child.stdout?.on('data', (chunk: Buffer) => collect(stdout, chunk));
  child.stderr?.on('data', (chunk: Buffer) => collect(stderr, chunk));
  const result = await waitForChild(child, {
    milliseconds: timeoutMs,
    description: `'${executable} --version'`,
  });
  if (bytes > 64 * 1024) throw new LedgerDelegateError('Ledger version output exceeded 64 KiB; repair the installed Ledger executable explicitly.', 69);
  const output = Buffer.concat(stdout).toString('utf8').trim();
  const detail = Buffer.concat(stderr).toString('utf8').trim();
  if (result.signal !== null) {
    throw new LedgerDelegateError(`Ledger readiness probe terminated by ${result.signal}; repair '${executable}' explicitly and retry.`, 69);
  }
  if (result.code !== 0) {
    throw new LedgerDelegateError(
      `yylo: '${executable} --version' failed with exit ${result.code ?? 1}` +
        `${detail ? `: ${detail}` : ''}. Reinstall yylo-ledger and retry.`,
      69,
    );
  }
  return output;
}

function terminateWithSignal(signal: NodeJS.Signals): never {
  // YYLO owns generic signal handlers. Remove them only at terminal handoff so
  // the delegate itself has the same signal outcome as the canonical executable.
  for (const forwarded of SIGNALS) process.removeAllListeners(forwarded);
  process.kill(process.pid, signal);
  const number = osConstants.signals[signal] ?? 1;
  process.exit(128 + number);
}

export async function checkLedgerReadiness(
  options: {
    readonly cwd?: string;
    readonly env?: NodeJS.ProcessEnv;
    readonly executableName?: string;
    readonly versionTimeoutMs?: number;
  } = {},
): Promise<string> {
  const cwd = options.cwd ?? process.cwd();
  // The wrapper's preflight marker is scoped to its probe subprocess, so the
  // delegated process can receive an exact copy of the actual caller environment.
  const env = { ...(options.env ?? process.env) };
  const executable = discoverLedgerExecutable(env, options.executableName, cwd);
  const output = await readVersion(
    executable,
    cwd,
    env,
    options.versionTimeoutMs ?? VERSION_HANDSHAKE_TIMEOUT_MS,
  );
  const version = parseVersion(output);
  if (version === undefined || !isCompatibleVersion(version)) {
    throw new LedgerDelegateError(
      `yylo: incompatible YYLO Ledger version from '${executable}' (cwd: ${cwd}): ` +
        `${output || 'unknown'} (required ${LEDGER_VERSION_RANGE}). ` +
        `Install a compatible yylo-ledger and retry.`,
      69,
    );
  }

  return executable;
}

export async function invokeLedger(
  args: readonly string[],
  options: Parameters<typeof checkLedgerReadiness>[0] = {},
): Promise<LedgerDelegateResult> {
  const cwd = options.cwd ?? process.cwd();
  let env = { ...(options.env ?? process.env) };
  let delegatedArgs = [...args];
  if (hasSimpleWorkspaceHint(cwd)) {
    const resolution = resolveController(cwd, 'kanban', { env });
    if (!resolution.valid || resolution.role !== 'simple') throw new Error('Invalid Simple Ledger authority.');
    if (args.some(arg => arg.startsWith('-c') || (arg.startsWith('--c') && '--config'.startsWith(arg.split('=')[0]!)))) {
      throw new Error('Simple Ledger uses the root-bound config; --config overrides are unsupported.');
    }
    env = buildChildProcessEnvironment(env, {
      JUNO_TASK_ROOT: resolution.path,
      JUNO_WORKSPACE_ROLE: 'simple',
    });
    delegatedArgs = ['--config', path.join(resolution.path, '.juno_task/config.json'), ...args];
  }
  const executable = await checkLedgerReadiness({ ...options, cwd, env });
  const child = spawn(executable, delegatedArgs, { cwd, env, stdio: 'inherit' });
  return waitForChild(child);
}

export async function runLedgerDelegate(args: readonly string[]): Promise<void> {
  try {
    const result = await invokeLedger(args);
    if (result.signal !== null) terminateWithSignal(result.signal);
    process.exitCode = result.code ?? 1;
  } catch (error) {
    if (error instanceof LedgerDelegateError) {
      process.stderr.write(`${error.message}\n`);
      process.exitCode = error.exitCode;
      return;
    }
    const detail = error instanceof Error ? error.message : String(error);
    process.stderr.write(`yylo: failed to delegate to yylo-ledger: ${detail}\n`);
    process.exitCode = 126;
  }
}

export function configureLedgerCommand(program: Command): void {
  const command = program
    .command('ledger [args...]')
    .description('Delegate transparently to an independently installed YYLO Ledger CLI')
    .allowUnknownOption(true)
    .allowExcessArguments(true)
    .action(runLedgerDelegate);
  // Help belongs to the standalone product. The explicit-input preflight must
  // preserve -h/--help in the untouched delegated argument tail too.
  markTransparentDelegate(command);
}
