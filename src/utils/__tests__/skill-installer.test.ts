import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import * as os from 'node:os';
import * as path from 'node:path';
import { SkillInstaller } from '../skill-installer.js';

const GROUPS = ['.agents/skills', '.claude/skills', '.pi/skills'];
const SKILLS = ['kanban-workflow', 'plan-kanban-tasks', 'ralph-loop', 'understand-project'];

type Runner = (command: string, args: string[], cwd?: string) => Promise<{ stdout: string; stderr: string }>;

describe('SkillInstaller remote acquisition', () => {
  let project: string;
  let runner: ReturnType<typeof vi.spyOn>;

  const populateNpxStage = async (stage: string, marker = 'canonical') => {
    for (const group of GROUPS) {
      for (const skill of SKILLS) {
        const root = path.join(stage, group, skill);
        await fs.ensureDir(root);
        await fs.writeFile(path.join(root, 'SKILL.md'), `---\nname: ${skill}\n---\n${marker}\n`);
        await fs.writeFile(path.join(root, 'README.md'), `# ${skill}\n`);
        if (skill === 'ralph-loop') {
          await fs.ensureDir(path.join(root, 'scripts'));
          await fs.writeFile(path.join(root, 'scripts', 'kanban.sh'), '#!/bin/sh\n', { mode: 0o755 });
        }
      }
    }
  };

  const populateClone = async (clone: string) => {
    for (const skill of SKILLS) {
      const root = path.join(clone, 'skills', skill);
      await fs.ensureDir(root);
      await fs.writeFile(path.join(root, 'SKILL.md'), `---\nname: ${skill}\n---\ncanonical\n`);
      await fs.writeFile(path.join(root, 'README.md'), `# ${skill}\n`);
      if (skill === 'ralph-loop') {
        await fs.ensureDir(path.join(root, 'scripts'));
        await fs.writeFile(path.join(root, 'scripts', 'kanban.sh'), '#!/bin/sh\n', { mode: 0o755 });
      }
    }
  };

  const defaultRunner: Runner = async (command, args, cwd) => {
    if (command === 'git' && args.includes('ls-files')) return { stdout: '', stderr: '' };
    if (command === 'git' && args.includes('ls-remote')) {
      return {
        stdout: args.includes('--refs')
          ? [
              'a\trefs/tags/v0.9.0',
              'b\trefs/tags/v1.0.0-rc.1',
              'c\trefs/tags/v1.0.0',
            ].join('\n') + '\n'
          : 'c\trefs/tags/v1.0.0\n',
        stderr: '',
      };
    }
    if (command === 'npx') {
      await populateNpxStage(cwd!);
      return { stdout: 'installed', stderr: '' };
    }
    if (command === 'git' && args[0] === 'clone') {
      await populateClone(args.at(-1)!);
      return { stdout: '', stderr: '' };
    }
    throw new Error(`unexpected command: ${command} ${args.join(' ')}`);
  };

  beforeEach(async () => {
    project = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-skill-installer-test-'));
    await fs.ensureDir(path.join(project, '.juno_task'));
    runner = vi
      .spyOn(SkillInstaller as unknown as { runCommand: Runner }, 'runCommand')
      .mockImplementation(defaultRunner);
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    await fs.remove(project);
  });

  it('resolves the latest stable SemVer and stages one targeted npx copy install', async () => {
    const result = await SkillInstaller.installRemote(project);
    expect(result).toEqual({ changed: true, version: 'v1.0.0', acquisition: 'npx' });
    const npx = runner.mock.calls.find(([command]) => command === 'npx');
    expect(npx?.[1]).toEqual(expect.arrayContaining([
      '--yes', 'skills', 'add',
      'https://github.com/yylo-dev/yylo-skills/tree/v1.0.0',
      '--copy', '--agent', 'codex', 'claude-code', 'pi',
    ]));
    for (const group of GROUPS) {
      for (const skill of SKILLS) {
        expect(await fs.pathExists(path.join(project, group, skill, 'SKILL.md'))).toBe(true);
      }
    }
  });

  it('normalizes and verifies an exact stable version', async () => {
    const result = await SkillInstaller.installRemote(project, { version: '1.0.0' });
    expect(result.version).toBe('v1.0.0');
    expect(runner.mock.calls.some(([command, args]) =>
      command === 'git' && args.includes('refs/tags/v1.0.0'))).toBe(true);
    await expect(SkillInstaller.installRemote(project, { version: '1.0.0-rc.1' }))
      .rejects.toThrow('Invalid stable skill version');
  });

  it('falls back to a shallow exact-tag clone only after npx fails', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') throw new Error('npx unavailable');
      return defaultRunner(command, args, cwd);
    });
    const result = await SkillInstaller.installRemote(project, { version: 'v1.0.0' });
    expect(result.acquisition).toBe('git');
    const clone = runner.mock.calls.find(([command, args]) => command === 'git' && args[0] === 'clone');
    expect(clone?.[1]).toEqual(expect.arrayContaining(['--depth', '1', '--branch', 'v1.0.0', '--single-branch']));
  });

  it('fails without destination mutation when both acquisition methods fail', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx' || (command === 'git' && args[0] === 'clone')) throw new Error('offline');
      return defaultRunner(command, args, cwd);
    });
    await expect(SkillInstaller.installRemote(project, { version: '1.0.0' }))
      .rejects.toThrow('Skill acquisition failed with npx');
    for (const group of GROUPS) expect(await fs.pathExists(path.join(project, group))).toBe(false);
  });

  it('rejects noncanonical staged paths and symbolic links', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') {
        await populateNpxStage(cwd!);
        await fs.ensureDir(path.join(cwd!, '.agents/skills/unexpected'));
        return { stdout: '', stderr: '' };
      }
      if (command === 'git' && args[0] === 'clone') throw new Error('fallback disabled');
      return defaultRunner(command, args, cwd);
    });
    await expect(SkillInstaller.installRemote(project, { version: '1.0.0' }))
      .rejects.toThrow('not the canonical four-skill set');

    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') {
        await populateNpxStage(cwd!);
        await fs.symlink('/tmp', path.join(cwd!, '.pi/skills/ralph-loop/escape'));
        return { stdout: '', stderr: '' };
      }
      if (command === 'git' && args[0] === 'clone') throw new Error('fallback disabled');
      return defaultRunner(command, args, cwd);
    });
    await expect(SkillInstaller.installRemote(project, { version: '1.0.0' }))
      .rejects.toThrow('symbolic link');
  });

  it('preflights every conflict before writing anything', async () => {
    const conflict = path.join(project, '.pi/skills/understand-project/SKILL.md');
    await fs.outputFile(conflict, 'owner bytes\n');
    await fs.outputFile(path.join(project, '.agents/skills/unrelated/SKILL.md'), 'unrelated\n');

    await expect(SkillInstaller.installRemote(project, { version: '1.0.0' }))
      .rejects.toThrow('Skill conflict at .pi/skills/understand-project');
    expect(await fs.readFile(conflict, 'utf8')).toBe('owner bytes\n');
    expect(await fs.readFile(path.join(project, '.agents/skills/unrelated/SKILL.md'), 'utf8'))
      .toBe('unrelated\n');
    expect(await fs.pathExists(path.join(project, '.agents/skills/kanban-workflow'))).toBe(false);
    expect(await fs.pathExists(path.join(project, '.juno_task/runtime/skills-install.json'))).toBe(false);
  });

  it('force replaces only canonical skill directories and preserves unrelated skills', async () => {
    const conflict = path.join(project, '.pi/skills/understand-project/SKILL.md');
    const unrelated = path.join(project, '.agents/skills/unrelated/SKILL.md');
    await fs.outputFile(conflict, 'owner bytes\n');
    await fs.outputFile(unrelated, 'unrelated\n');

    await SkillInstaller.installRemote(project, { version: '1.0.0', force: true });
    expect(await fs.readFile(conflict, 'utf8')).toContain('name: understand-project');
    expect(await fs.readFile(unrelated, 'utf8')).toBe('unrelated\n');
    const record = await SkillInstaller.getInstallRecord(project);
    expect(record).toMatchObject({ version: 'v1.0.0', acquisition: 'npx', skills: SKILLS });
  });

  it('keeps list and status inspection local with no command execution', async () => {
    await SkillInstaller.installRemote(project, { version: '1.0.0' });
    runner.mockClear();
    expect(await SkillInstaller.needsUpdate(project)).toBe(false);
    expect(await SkillInstaller.listSkillGroups(project)).toHaveLength(3);
    expect(await SkillInstaller.getInstallRecord(project)).toMatchObject({ version: 'v1.0.0' });
    expect(runner).not.toHaveBeenCalled();

    await fs.writeFile(
      path.join(project, '.agents/skills/kanban-workflow/SKILL.md'),
      'locally changed\n',
    );
    expect(await SkillInstaller.needsUpdate(project)).toBe(true);
    expect(runner).not.toHaveBeenCalled();
  });

  it('enforces metadata-controller ignored-surface safety before networking', async () => {
    await fs.writeJson(path.join(project, '.juno_task/config.json'), {
      controllerWorkspace: {
        mode: 'metadata-only',
        policy: '.juno_task/config/metadata-controller.json',
      },
    });
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow(
      'requires the reviewed ignored-runtime policy',
    );
    expect(runner).not.toHaveBeenCalled();
  });

  it('creates Pi settings once and preserves user settings', async () => {
    await SkillInstaller.installRemote(project, { version: '1.0.0' });
    expect(await fs.readJson(path.join(project, '.pi/settings.json')))
      .toEqual({ skills: ['.claude/skills'], quietStartup: true });
    const custom = { theme: 'dark', skills: ['/owner/skills'] };
    await fs.writeJson(path.join(project, '.pi/settings.json'), custom);
    await SkillInstaller.installRemote(project, { version: '1.0.0', force: true });
    expect(await fs.readJson(path.join(project, '.pi/settings.json'))).toEqual(custom);
  });
});
