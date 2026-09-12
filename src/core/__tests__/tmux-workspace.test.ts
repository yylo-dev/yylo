import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import { randomUUID } from 'node:crypto';
import { afterEach, describe, expect, it } from 'vitest';
import {
  COMPLETION_OPTION,
  LEGACY_UNSEEN_OPTION,
  MONITOR_OWNER_OPTION,
  TMUX_WORKSPACE_SCHEMA,
  TmuxCompletionMonitor,
  TmuxWorkspace,
  TmuxWorkspaceError,
  UNSEEN_OPTION,
  type TmuxRunOptions,
  type TmuxRunResult,
} from '../tmux-workspace.js';

class FakeTmux {
  calls: string[][] = [];
  exists = false;
  owner = '';
  commands = new Map<string, string>([['%1', 'bash']]);
  attached = 0;
  windows = [{ id: '@1', index: 0, name: 'main', completion: '', unseen: '', legacy: '' }];

  run = (args: readonly string[], _options: TmuxRunOptions): TmuxRunResult => {
    const argv = [...args];
    this.calls.push(argv);
    if (argv[0] === 'has-session') return { status: this.exists ? 0 : 1, stdout: '' };
    if (argv[0] === 'new-session') {
      this.exists = true;
      return { status: 0, stdout: '' };
    }
    if (!this.exists) return { status: 1, stdout: '' };
    if (argv[0] === 'list-panes') {
      const output = this.windows
        .map((window) =>
          [
            'safe',
            window.id,
            String(window.index),
            window.name,
            `%${window.index + 1}`,
            '0',
            window.name,
            '1',
            this.commands.get(`%${window.index + 1}`) ?? 'bash',
            window.index === 0 ? '1' : '0',
            String(this.attached),
            window.completion,
            window.unseen,
            window.legacy,
          ].join('\t'),
        )
        .join('\n');
      return { status: 0, stdout: output + '\n' };
    }
    if (argv[0] === 'new-window') {
      const name = argv[argv.indexOf('-n') + 1] ?? '';
      this.windows.push({ id: '@2', index: 1, name, completion: '', unseen: '', legacy: '' });
      return { status: 0, stdout: '@2\n' };
    }
    if (argv[0] === 'show-options' && argv.at(-1) === MONITOR_OWNER_OPTION)
      return this.owner ? { status: 0, stdout: this.owner + '\n' } : { status: 1, stdout: '' };
    if (argv[0] === 'if-shell') {
      const condition = argv[argv.indexOf('-t') + 2] ?? '';
      const action = argv[argv.indexOf('-t') + 3] ?? '';
      const match = /^#\{==:#\{(@[^}]+)\},([^}]*)\}$/.exec(condition);
      if (!match) return { status: 1, stdout: '' };
      const target = argv[argv.indexOf('-t') + 1];
      const window = this.windows.find((item) => item.id === target);
      const current = match[1] === MONITOR_OWNER_OPTION ? this.owner
        : match[1] === UNSEEN_OPTION ? window?.unseen
        : match[1] === LEGACY_UNSEEN_OPTION ? window?.legacy : undefined;
      if (current === match[2]) return this.run(action.split(' '), _options);
      return { status: 0, stdout: '' };
    }
    if (argv[0] === 'set-option') {
      const targetIndex = argv.indexOf('-t');
      const option = argv[targetIndex + 2];
      const unset = argv.includes('-wu') || argv.includes('-u');
      const value = unset ? '' : (argv[targetIndex + 3] ?? '');
      if (option === MONITOR_OWNER_OPTION) {
        this.owner = value;return { status: 0, stdout: '' };
      }
      const window = this.windows.find((item) => item.id === argv[targetIndex + 1]);
      if (!window) return { status: 1, stdout: '' };
      if (option === COMPLETION_OPTION) window.completion = value;
      if (option === UNSEEN_OPTION) window.unseen = value;
      if (option === LEGACY_UNSEEN_OPTION) window.legacy = value;
      return { status: 0, stdout: '' };
    }
    if (argv[0] === 'select-window' || argv[0] === 'attach-session' || argv[0] === 'switch-client')
      return { status: 0, stdout: '' };
    return { status: 1, stdout: '' };
  };
}

describe('TmuxWorkspace', () => {
  it('creates and ensures without replacing an existing session', () => {
    const fake = new FakeTmux();
    const gateway = new TmuxWorkspace(fake.run);
    const created = gateway.createSession('safe', os.tmpdir());
    expect(created.schemaVersion).toBe(TMUX_WORKSPACE_SCHEMA);
    gateway.ensureSession('safe', os.tmpdir());
    expect(fake.calls.filter((call) => call[0] === 'new-session')).toHaveLength(1);
    expect(() => gateway.createSession('safe', os.tmpdir())).toThrow('already exists');
  });

  it('creates one uniquely named window with an argv-only canonical cwd', () => {
    const fake = new FakeTmux();
    fake.exists = true;
    const gateway = new TmuxWorkspace(fake.run);
    const window = gateway.createWindow('safe', 'API fixes', os.tmpdir(), true);
    expect(window.name).toBe('API fixes');
    expect(fake.calls).toContainEqual(
      expect.arrayContaining(['new-window', '-n', 'API fixes', '-c', os.tmpdir()]),
    );
    expect(fake.calls).toContainEqual(['select-window', '-t', '@2']);
    expect(() => gateway.createWindow('safe', 'API fixes', os.tmpdir())).toThrow('already exists');
  });

  it('reports legacy unread state without clearing and compare-clears exact generations', () => {
    const fake = new FakeTmux();
    fake.exists = true;
    fake.windows[0]!.legacy = 'abcdefghijklmnop';
    const gateway = new TmuxWorkspace(fake.run);
    expect(gateway.status('safe').windows[0]).toMatchObject({
      unseenGeneration: 'abcdefghijklmnop',
      legacyUnseen: true,
    });
    expect(fake.calls.some((call) => call[0] === 'set-option')).toBe(false);
    expect(gateway.clearUnread('safe', 'main', 'othergeneration1')).toBe(false);
    expect(fake.windows[0]!.legacy).toBe('abcdefghijklmnop');
    expect(gateway.clearUnread('safe', 'main', 'abcdefghijklmnop')).toBe(true);
    const generation = gateway.markUnread('safe', '0');
    expect(generation).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
    expect(fake.windows[0]).toMatchObject({ completion: generation, unseen: generation });
    expect(gateway.clearUnread('safe', '@1', 'abcdefghijklmnop')).toBe(false);
    expect(fake.windows[0]!.unseen).toBe(generation);
    expect(gateway.clearUnread('safe', '@1', generation)).toBe(true);
  });

  it('uses switch-client inside tmux, attach outside, and refuses non-TTY use', () => {
    const fake = new FakeTmux();
    fake.exists = true;
    const gateway = new TmuxWorkspace(fake.run);
    gateway.connect('safe', { insideTmux: true, tty: true });
    gateway.connect('safe', { insideTmux: false, tty: true });
    expect(fake.calls).toContainEqual(['switch-client', '-t', 'safe']);
    expect(fake.calls).toContainEqual(['attach-session', '-t', 'safe']);
    expect(() => gateway.connect('safe', { tty: false })).toThrow('interactive terminal');
  });

  it('rejects unsafe names, unavailable directories, and malformed inventory', () => {
    const fake = new FakeTmux();
    fake.exists = true;
    const gateway = new TmuxWorkspace(fake.run);
    expect(() => gateway.status('../other')).toThrow(TmuxWorkspaceError);
    expect(() => gateway.createWindow('safe', 'bad:name', os.tmpdir())).toThrow('invalid');
    expect(() => gateway.createSession('new', '/definitely/missing')).toThrow('unavailable');
    fake.run = () => ({ status: 0, stdout: 'malformed\n' });
    expect(() => new TmuxWorkspace(fake.run).status('safe')).toThrow('malformed');
  });
});

describe('TmuxCompletionMonitor', () => {
  it('initializes silently and publishes one background generation on busy-to-shell', () => {
    const fake = new FakeTmux();fake.exists = true;fake.commands.set('%1', 'node');
    const gateway = new TmuxWorkspace(fake.run);const monitor = new TmuxCompletionMonitor(gateway, 'safe');
    monitor.pollOnce();
    expect(fake.windows[0]).toMatchObject({ completion: '', unseen: '' });
    fake.commands.set('%1', 'bash');monitor.pollOnce();
    expect(fake.windows[0]!.completion).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
    expect(fake.windows[0]!.unseen).toBe(fake.windows[0]!.completion);
    monitor.pollOnce();
    expect(fake.calls.filter((call) => call[0] === 'set-option' && call.includes(COMPLETION_OPTION))).toHaveLength(1);
  });

  it('publishes seen completion for an attached active window and acknowledges old state', () => {
    const fake = new FakeTmux();fake.exists = true;fake.attached = 1;
    fake.windows[0]!.unseen = 'abcdefghijklmnop';fake.commands.set('%1', 'node');
    const gateway = new TmuxWorkspace(fake.run);const monitor = new TmuxCompletionMonitor(gateway, 'safe');
    monitor.pollOnce();
    expect(fake.windows[0]!.unseen).toBe('');
    fake.commands.set('%1', 'bash');monitor.pollOnce();
    expect(fake.windows[0]!.completion).not.toBe('');expect(fake.windows[0]!.unseen).toBe('');
  });

  it('fences competing owners and releases only its own token', () => {
    const fake = new FakeTmux();fake.exists = true;const gateway = new TmuxWorkspace(fake.run);
    const owner = gateway.claimMonitor('safe');
    expect(gateway.ownsMonitor('safe', owner)).toBe(true);
    expect(() => gateway.claimMonitor('safe')).toThrow('another tmux workspace monitor');
    expect(() => gateway.releaseMonitor('safe', 'differentowner123')).not.toThrow();
    expect(gateway.ownsMonitor('safe', owner)).toBe(true);
    gateway.releaseMonitor('safe', owner);expect(gateway.monitorOwner('safe')).toBeNull();
  });

  it('forgets removed panes without manufacturing completions', () => {
    const fake = new FakeTmux();fake.exists = true;fake.commands.set('%1', 'node');
    const gateway = new TmuxWorkspace(fake.run);const monitor = new TmuxCompletionMonitor(gateway, 'safe');
    monitor.pollOnce();fake.windows.splice(0, 1);monitor.pollOnce();
    fake.windows.push({ id: '@2', index: 1, name: 'new', completion: '', unseen: '', legacy: '' });
    fake.commands.set('%2', 'bash');monitor.pollOnce();
    expect(fake.windows[0]).toMatchObject({ completion: '', unseen: '' });
  });
});

const realSessions: string[] = [];
afterEach(() => {
  for (const session of realSessions.splice(0)) spawnTmux(['kill-session', '-t', session]);
});

function spawnTmux(args: string[]): void {
  if (spawnSync('tmux', args, { stdio: 'ignore' }).status !== 0)
    throw new Error('tmux test operation failed');
}

describe.skipIf(!existsSync('/usr/bin/tmux'))('TmuxWorkspace disposable tmux', () => {
  it('creates a session and named window and preserves generation state', () => {
    const session = `yylo-test-${randomUUID().slice(0, 8)}`;
    realSessions.push(session);
    const gateway = new TmuxWorkspace();
    gateway.createSession(session, os.tmpdir());
    gateway.createWindow(session, 'named window', os.tmpdir());
    const generation = gateway.markUnread(session, 'named window');
    expect(
      gateway.status(session).windows.find((item) => item.name === 'named window')
        ?.unseenGeneration,
    ).toBe(generation);
    expect(gateway.clearUnread(session, 'named window', 'stalegeneration1')).toBe(false);
    expect(gateway.clearUnread(session, 'named window', generation)).toBe(true);
  });
});
