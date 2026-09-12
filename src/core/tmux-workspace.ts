import { randomBytes } from 'node:crypto';
import { realpathSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

export const TMUX_WORKSPACE_SCHEMA = 'yylo.tmux-workspace.v1';
export const COMPLETION_OPTION = '@yylo_workspace_completion';
export const UNSEEN_OPTION = '@yylo_workspace_unseen';
export const LEGACY_UNSEEN_OPTION = '@telegram_tmux_unseen';

const SESSION = /^[A-Za-z0-9_-]{1,64}$/;
const WINDOW = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$/;
const WINDOW_ID = /^@\d+$/;
const PANE_ID = /^%\d+$/;
const GENERATION = /^[A-Za-z0-9_-]{16,128}$/;
const INVENTORY_FORMAT = [
  '#{session_name}',
  '#{window_id}',
  '#{window_index}',
  '#{window_name}',
  '#{pane_id}',
  '#{pane_index}',
  '#{pane_title}',
  '#{pane_active}',
  '#{pane_current_command}',
  '#{window_active}',
  '#{session_attached}',
  `#{${COMPLETION_OPTION}}`,
  `#{${UNSEEN_OPTION}}`,
  `#{${LEGACY_UNSEEN_OPTION}}`,
].join('\t');

export interface TmuxRunOptions {
  interactive?: boolean;
  timeoutMs?: number;
}
export interface TmuxRunResult {
  status: number | null;
  stdout?: string;
  error?: Error;
}
export type TmuxRunner = (args: readonly string[], options: TmuxRunOptions) => TmuxRunResult;

export interface TmuxPaneStatus {
  session: string;
  windowId: string;
  windowIndex: number;
  windowName: string;
  paneId: string;
  paneIndex: number;
  paneTitle: string;
  paneActive: boolean;
  currentCommand: string;
  windowActive: boolean;
  attachedClients: number;
  completionGeneration: string | null;
  unseenGeneration: string | null;
  canonicalUnseenGeneration: string | null;
  legacyUnseenGeneration: string | null;
  legacyUnseen: boolean;
}

export interface TmuxWorkspaceStatus {
  schemaVersion: typeof TMUX_WORKSPACE_SCHEMA;
  session: string;
  attachedClients: number;
  windows: Array<{
    id: string;
    index: number;
    name: string;
    active: boolean;
    completionGeneration: string | null;
    unseenGeneration: string | null;
    legacyUnseen: boolean;
    panes: Array<{
      id: string;
      index: number;
      title: string;
      active: boolean;
      currentCommand: string;
    }>;
  }>;
}

function defaultRunner(args: readonly string[], options: TmuxRunOptions): TmuxRunResult {
  const interactive = options.interactive === true;
  const result = spawnSync('tmux', [...args], {
    encoding: 'utf8',
    timeout: interactive ? undefined : (options.timeoutMs ?? 10_000),
    maxBuffer: 1024 * 1024,
    stdio: interactive ? 'inherit' : ['ignore', 'pipe', 'pipe'],
  });
  return {
    status: result.status,
    ...(typeof result.stdout === 'string' ? { stdout: result.stdout } : {}),
    ...(result.error instanceof Error ? { error: result.error } : {}),
  };
}

export class TmuxWorkspaceError extends Error {
  constructor(message = 'tmux workspace operation failed') {
    super(message);
    this.name = 'TmuxWorkspaceError';
  }
}

export class TmuxWorkspace {
  constructor(private readonly runner: TmuxRunner = defaultRunner) {}

  static sessionName(value: string): string {
    if (!SESSION.test(value)) throw new TmuxWorkspaceError('tmux session name is invalid');
    return value;
  }

  static windowName(value: string): string {
    if (!WINDOW.test(value)) throw new TmuxWorkspaceError('tmux window name is invalid');
    return value;
  }

  static generation(value: string): string {
    if (!GENERATION.test(value)) throw new TmuxWorkspaceError('tmux generation is invalid');
    return value;
  }

  static directory(value: string): string {
    try {
      const resolved = realpathSync(value);
      if (!statSync(resolved).isDirectory()) throw new Error('not a directory');
      return resolved;
    } catch {
      throw new TmuxWorkspaceError('tmux working directory is unavailable');
    }
  }

  private invoke(args: readonly string[], options: TmuxRunOptions = {}): string {
    const result = this.runner(args, { timeoutMs: 10_000, ...options });
    if (result.error || result.status !== 0) throw new TmuxWorkspaceError();
    const stdout = result.stdout ?? '';
    if (Buffer.byteLength(stdout, 'utf8') > 1024 * 1024)
      throw new TmuxWorkspaceError('tmux workspace response is too large');
    return stdout;
  }

  hasSession(name: string): boolean {
    name = TmuxWorkspace.sessionName(name);
    const result = this.runner(['has-session', '-t', name], { timeoutMs: 10_000 });
    if (result.error || result.status === null) throw new TmuxWorkspaceError();
    return result.status === 0;
  }

  createSession(name: string, cwd: string, initialWindow = 'main'): TmuxWorkspaceStatus {
    name = TmuxWorkspace.sessionName(name);
    cwd = TmuxWorkspace.directory(cwd);
    initialWindow = TmuxWorkspace.windowName(initialWindow);
    if (this.hasSession(name)) throw new TmuxWorkspaceError('tmux session already exists');
    this.invoke(['new-session', '-d', '-s', name, '-n', initialWindow, '-c', cwd]);
    return this.status(name);
  }

  ensureSession(name: string, cwd: string, initialWindow = 'main'): TmuxWorkspaceStatus {
    name = TmuxWorkspace.sessionName(name);
    cwd = TmuxWorkspace.directory(cwd);
    initialWindow = TmuxWorkspace.windowName(initialWindow);
    if (this.hasSession(name)) return this.status(name);
    try {
      return this.createSession(name, cwd, initialWindow);
    } catch (error) {
      if (this.hasSession(name)) return this.status(name);
      throw error;
    }
  }

  connect(name: string, context: { insideTmux?: boolean; tty?: boolean } = {}): void {
    name = TmuxWorkspace.sessionName(name);
    if (!this.hasSession(name)) throw new TmuxWorkspaceError('tmux session is unavailable');
    const insideTmux = context.insideTmux ?? Boolean(process.env.TMUX);
    const tty = context.tty ?? Boolean(process.stdin.isTTY && process.stdout.isTTY);
    if (!tty) throw new TmuxWorkspaceError('tmux connection requires an interactive terminal');
    if (insideTmux) this.invoke(['switch-client', '-t', name], { interactive: true });
    else this.invoke(['attach-session', '-t', name], { interactive: true });
  }

  open(
    name: string,
    cwd: string,
    initialWindow = 'main',
    context: { insideTmux?: boolean; tty?: boolean } = {},
  ): void {
    this.ensureSession(name, cwd, initialWindow);
    this.connect(name, context);
  }

  panes(name: string): TmuxPaneStatus[] {
    name = TmuxWorkspace.sessionName(name);
    const text = this.invoke(['list-panes', '-s', '-t', name, '-F', INVENTORY_FORMAT]);
    const lines = text.split('\n').filter(Boolean);
    if (lines.length > 1000) throw new TmuxWorkspaceError('tmux workspace response is too large');
    return lines.map((line) => {
      const fields = line.split('\t');
      if (fields.length !== 14)
        throw new TmuxWorkspaceError('tmux workspace inventory is malformed');
      const [
        session,
        windowId,
        windowIndex,
        windowName,
        paneId,
        paneIndex,
        paneTitle,
        paneActive,
        currentCommand,
        windowActive,
        attachedClients,
        completion,
        unseen,
        legacy,
      ] = fields;
      if (
        session !== name ||
        !windowId ||
        !WINDOW_ID.test(windowId) ||
        !paneId ||
        !PANE_ID.test(paneId) ||
        !/^\d+$/.test(windowIndex ?? '') ||
        !/^\d+$/.test(paneIndex ?? '') ||
        !/^[01]$/.test(paneActive ?? '') ||
        !/^[01]$/.test(windowActive ?? '') ||
        !/^\d+$/.test(attachedClients ?? '') ||
        !currentCommand ||
        (completion && !GENERATION.test(completion)) ||
        (unseen && !GENERATION.test(unseen)) ||
        (legacy && !GENERATION.test(legacy))
      )
        throw new TmuxWorkspaceError('tmux workspace inventory is malformed');
      return {
        session,
        windowId,
        windowIndex: Number(windowIndex),
        windowName: windowName ?? '',
        paneId,
        paneIndex: Number(paneIndex),
        paneTitle: paneTitle ?? '',
        paneActive: paneActive === '1',
        currentCommand,
        windowActive: windowActive === '1',
        attachedClients: Number(attachedClients),
        completionGeneration: completion || null,
        unseenGeneration: unseen || legacy || null,
        canonicalUnseenGeneration: unseen || null,
        legacyUnseenGeneration: legacy || null,
        legacyUnseen: Boolean(legacy),
      };
    });
  }

  status(name: string): TmuxWorkspaceStatus {
    const panes = this.panes(name);
    const windows = new Map<string, TmuxWorkspaceStatus['windows'][number]>();
    for (const pane of panes) {
      let window = windows.get(pane.windowId);
      if (!window) {
        window = {
          id: pane.windowId,
          index: pane.windowIndex,
          name: pane.windowName,
          active: pane.windowActive,
          completionGeneration: pane.completionGeneration,
          unseenGeneration: pane.unseenGeneration,
          legacyUnseen: pane.legacyUnseen,
          panes: [],
        };
        windows.set(pane.windowId, window);
      }
      window.panes.push({
        id: pane.paneId,
        index: pane.paneIndex,
        title: pane.paneTitle,
        active: pane.paneActive,
        currentCommand: pane.currentCommand,
      });
    }
    const ordered = [...windows.values()].sort((a, b) => a.index - b.index);
    for (const window of ordered) window.panes.sort((a, b) => a.index - b.index);
    return {
      schemaVersion: TMUX_WORKSPACE_SCHEMA,
      session: TmuxWorkspace.sessionName(name),
      attachedClients: panes[0]?.attachedClients ?? 0,
      windows: ordered,
    };
  }

  createWindow(
    session: string,
    name: string,
    cwd: string,
    select = false,
  ): TmuxWorkspaceStatus['windows'][number] {
    session = TmuxWorkspace.sessionName(session);
    name = TmuxWorkspace.windowName(name);
    cwd = TmuxWorkspace.directory(cwd);
    if (this.status(session).windows.some((item) => item.name === name))
      throw new TmuxWorkspaceError('tmux window already exists');
    const windowId = this.invoke([
      'new-window',
      '-d',
      '-P',
      '-F',
      '#{window_id}',
      '-t',
      `${session}:`,
      '-n',
      name,
      '-c',
      cwd,
    ]).trim();
    if (!WINDOW_ID.test(windowId)) throw new TmuxWorkspaceError();
    if (select) this.invoke(['select-window', '-t', windowId]);
    const created = this.status(session).windows.find((item) => item.id === windowId);
    if (!created) throw new TmuxWorkspaceError();
    return created;
  }

  private window(session: string, target: string): TmuxWorkspaceStatus['windows'][number] {
    session = TmuxWorkspace.sessionName(session);
    if (!WINDOW_ID.test(target) && !/^\d+$/.test(target) && !WINDOW.test(target))
      throw new TmuxWorkspaceError('tmux window target is invalid');
    const matches = this.status(session).windows.filter(
      (item) => item.id === target || String(item.index) === target || item.name === target,
    );
    const match = matches[0];
    if (matches.length !== 1 || !match)
      throw new TmuxWorkspaceError('tmux window is stale or ambiguous');
    return match;
  }

  markUnread(session: string, target: string): string {
    const window = this.window(session, target);
    const generation = randomBytes(18).toString('base64url');
    this.invoke(['set-option', '-w', '-t', window.id, COMPLETION_OPTION, generation]);
    this.invoke(['set-option', '-w', '-t', window.id, UNSEEN_OPTION, generation]);
    if (window.legacyUnseen)
      this.invoke(['set-option', '-wu', '-t', window.id, LEGACY_UNSEEN_OPTION]);
    return generation;
  }

  clearUnread(session: string, target: string, generation: string): boolean {
    generation = TmuxWorkspace.generation(generation);
    const window = this.window(session, target);
    const pane = this.panes(session).find((item) => item.windowId === window.id);
    if (!pane) throw new TmuxWorkspaceError('tmux window is stale or ambiguous');
    let cleared = false;
    if (pane.canonicalUnseenGeneration === generation) {
      this.invoke(['set-option', '-wu', '-t', window.id, UNSEEN_OPTION]);
      cleared = true;
    }
    if (pane.legacyUnseenGeneration === generation) {
      this.invoke(['set-option', '-wu', '-t', window.id, LEGACY_UNSEEN_OPTION]);
      cleared = true;
    }
    return cleared;
  }
}
