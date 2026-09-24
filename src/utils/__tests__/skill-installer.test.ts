import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import semver from 'semver';
import * as os from 'node:os';
import * as path from 'node:path';
import { SkillInstaller } from '../skill-installer.js';
import { createSkillsCommand } from '../../cli/commands/skills.js';
import { findSkillFile, expandSkillInvocation } from '../../templates/extensions/pi/juno-skill-preprocessor.js';

const GROUPS = ['.agents/skills', '.claude/skills', '.pi/skills'];
const GROUP_NAMES = ['codex', 'claude', 'pi'];
const SKILLS = [
  'artifact-yylo', 'benchmark-yylo', 'ledger-tasks-yylo', 'plan-ledger-tasks-yylo',
  'ralph-loop-yylo', 'understand-project-yylo', 'wiki-yylo', 'workflow-yylo',
];
const LEGACY = ['kanban-workflow', 'plan-kanban-tasks', 'ralph-loop', 'understand-project'];

type Runner = (command: string, args: string[], cwd?: string) => Promise<{ stdout: string; stderr: string }>;

describe('SkillInstaller remote acquisition', () => {
  let project: string;
  let runner: ReturnType<typeof vi.spyOn>;

  const populateNpxStage = async (stage: string, marker = 'canonical $ARGUMENTS') => {
    for (const group of GROUPS) {
      for (const skill of SKILLS) {
        const root = path.join(stage, group, skill);
        await fs.ensureDir(root);
        await fs.writeFile(path.join(root, 'SKILL.md'), `---\nname: ${skill}\n---\n${marker}\n`);
        await fs.writeFile(path.join(root, 'README.md'), `# ${skill}\n`);
        if (skill === 'ralph-loop-yylo') {
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
      await fs.writeFile(path.join(root, 'SKILL.md'), `---\nname: ${skill}\n---\ncanonical $ARGUMENTS\n`);
      await fs.writeFile(path.join(root, 'README.md'), `# ${skill}\n`);
    }
  };

  const defaultRunner: Runner = async (command, args, cwd) => {
    if (command === 'git' && args.includes('ls-files')) return { stdout: '', stderr: '' };
    if (command === 'git' && args.includes('ls-remote')) {
      return {
        stdout: args.includes('--refs')
          ? ['a\trefs/tags/v2.0.4', 'b\trefs/tags/v2.1.0-rc.1', 'c\trefs/tags/v2.1.0'].join('\n') + '\n'
          : 'c\trefs/tags/v2.1.0\n',
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

  const createRecordedLegacyInstall = async () => {
    const digests: Record<string, Record<string, string>> = {};
    const digest = (SkillInstaller as unknown as { directoryDigest(root: string): Promise<string> }).directoryDigest;
    for (let index = 0; index < GROUPS.length; index += 1) {
      digests[GROUP_NAMES[index]!] = {};
      for (const skill of LEGACY) {
        const root = path.join(project, GROUPS[index]!, skill);
        await fs.ensureDir(root);
        await fs.writeFile(path.join(root, 'SKILL.md'), `---\nname: ${skill}\n---\nlegacy\n`);
        digests[GROUP_NAMES[index]!]![skill] = await digest.call(SkillInstaller, root);
      }
    }
    await fs.outputJson(path.join(project, '.juno_task/runtime/skills-install.json'), {
      schemaVersion: 1,
      repository: SkillInstaller.REPOSITORY,
      version: 'v1.0.0',
      acquisition: 'npx',
      skills: LEGACY,
      digests,
      installedAt: '2026-01-01T00:00:00.000Z',
    });
  };

  beforeEach(async () => {
    project = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-skill-installer-test-'));
    await fs.ensureDir(path.join(project, '.juno_task'));
    runner = vi.spyOn(SkillInstaller as unknown as { runCommand: Runner }, 'runCommand')
      .mockImplementation(defaultRunner);
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    await fs.remove(project);
  });

  it('inspects independent skill receipts locally without claiming CLI-package ownership', async () => {
    await SkillInstaller.installRemote(project, { version: '2.1.0' });
    runner.mockClear();
    expect(await SkillInstaller.inspectGuidance(project)).toEqual({ coherent: true, version: 'v2.1.0', findings: [] });
    const legacy = '.pi/skills/ralph-loop/references/implement.md';
    await fs.outputFile(path.join(project, legacy), 'Run yy merge arbiter run TASK_ID\nReviewer A then Reviewer B\n');
    const report = await SkillInstaller.inspectGuidance(project);
    expect(report.coherent).toBe(false);
    expect(report.findings).toContainEqual({ destination: legacy, reason: 'retired-lifecycle' });
    expect(report.findings).toContainEqual({ destination: '.pi/skills/ralph-loop', reason: 'legacy-skill' });
    expect(await fs.readFile(path.join(project, legacy), 'utf8')).toContain('yy merge arbiter run');
    expect(runner).not.toHaveBeenCalled();
  });

  it('detects retired nested instructions even when the independent receipt matches', async () => {
    await SkillInstaller.installRemote(project, { version: '2.1.0' });
    const destination = '.claude/skills/ralph-loop-yylo/references/implement.md';
    await fs.outputFile(path.join(project, destination), 'yy merge drive TASK_ID\n');
    const receiptPath = path.join(project, '.juno_task/runtime/skills-install.json');
    const receipt = await fs.readJson(receiptPath);
    const digest = (SkillInstaller as unknown as { directoryDigest(root: string): Promise<string> }).directoryDigest;
    receipt.digests.claude['ralph-loop-yylo'] = await digest.call(SkillInstaller, path.join(project, '.claude/skills/ralph-loop-yylo'));
    await fs.writeJson(receiptPath, receipt);
    const before = await fs.readFile(receiptPath);
    runner.mockClear();
    expect(await SkillInstaller.inspectGuidance(project)).toMatchObject({
      coherent: false,
      findings: [{ destination, reason: 'retired-lifecycle' }],
    });
    expect(await fs.readFile(receiptPath)).toEqual(before);
    expect(runner).not.toHaveBeenCalled();
  });

  it('reports modified, missing and unverified skills without replacing bytes', async () => {
    await SkillInstaller.installRemote(project, { version: '2.1.0' });
    const root = '.pi/skills/ralph-loop-yylo';
    await fs.outputFile(path.join(project, root, 'references/local.md'), 'owner bytes');
    await fs.remove(path.join(project, '.agents/skills/wiki-yylo'));
    const receiptPath = path.join(project, '.juno_task/runtime/skills-install.json');
    const receipt = await fs.readJson(receiptPath);
    delete receipt.digests.claude['wiki-yylo'];
    await fs.writeJson(receiptPath, receipt);
    runner.mockClear();
    const report = await SkillInstaller.inspectGuidance(project);
    expect(report.findings).toEqual(expect.arrayContaining([
      { destination: root, reason: 'receipt-drift' },
      { destination: '.agents/skills/wiki-yylo', reason: 'receipt-drift' },
      { destination: '.claude/skills/wiki-yylo', reason: 'unverified' },
    ]));
    expect(await fs.readFile(path.join(project, root, 'references/local.md'), 'utf8')).toBe('owner bytes');
    expect(runner).not.toHaveBeenCalled();
  });

  it('refuses unsafe nested paths and bounds local inspection', async () => {
    const root = '.pi/skills/ralph-loop';
    await fs.ensureDir(path.join(project, root));
    await fs.symlink(os.tmpdir(), path.join(project, root, 'references'));
    const oversized = '.claude/skills/ralph-loop';
    await fs.outputFile(path.join(project, oversized, 'SKILL.md'), 'x'.repeat(1024 * 1024 + 1));
    const report = await SkillInstaller.inspectGuidance(project);
    expect(report.findings).toEqual(expect.arrayContaining([
      { destination: root, reason: 'unsafe-or-unreadable' },
      { destination: oversized, reason: 'unsafe-or-unreadable' },
    ]));
    expect(runner).not.toHaveBeenCalled();
  });

  it('does not reinterpret historical evidence or unrelated project skills as current policy', async () => {
    await fs.outputFile(path.join(project, '.juno_task/wiki/history.md'), 'yy merge arbiter run TASK_ID');
    await fs.outputFile(path.join(project, '.pi/skills/project-owned/SKILL.md'), 'yy merge drive TASK_ID');
    const report = await SkillInstaller.inspectGuidance(project);
    expect(report.coherent).toBe(false);
    expect(report.findings).toHaveLength(GROUPS.length * SKILLS.length);
    expect(report.findings.every((finding) => finding.reason === 'missing')).toBe(true);
    expect(report.findings.some((finding) => finding.destination.includes('project-owned'))).toBe(false);
    expect(runner).not.toHaveBeenCalled();
  });

  it('resolves latest stable SemVer and installs the exact eight targeted skills', async () => {
    expect(SkillInstaller.SKILLS).toEqual(SKILLS);
    expect(SkillInstaller.SKILLS).toHaveLength(8);
    const result = await SkillInstaller.installRemote(project);
    expect(result).toEqual({ changed: true, version: 'v2.1.0', acquisition: 'npx' });
    const npx = runner.mock.calls.find(([command]) => command === 'npx');
    expect(npx?.[1]).toEqual(expect.arrayContaining([
      '--yes', 'skills', 'add',
      'https://github.com/yylo-dev/yylo-skills/tree/v2.1.0',
      '--copy', '--agent', 'codex', 'claude-code', 'pi',
      ...SKILLS,
    ]));
    for (const group of GROUPS) for (const skill of SKILLS) {
      expect(await fs.pathExists(path.join(project, group, skill, 'SKILL.md'))).toBe(true);
      expect(await fs.readFile(path.join(project, group, skill, 'SKILL.md'), 'utf8')).toContain('$ARGUMENTS');
    }
  });

  it.each(['2.1.0', '2.1.1'])('normalizes compatible stable version %s and rejects prereleases', async (version) => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'git' && args.includes('ls-remote')) {
        return { stdout: `abc\trefs/tags/v${version}\n`, stderr: '' };
      }
      return defaultRunner(command, args, cwd);
    });
    expect((await SkillInstaller.installRemote(project, { version })).version).toBe(`v${version}`);
    expect(runner.mock.calls.some(([command, args]) =>
      command === 'git' && args.includes(`refs/tags/v${version}`))).toBe(true);
    expect(await SkillInstaller.inspectGuidance(project)).toEqual({ coherent: true, version: `v${version}`, findings: [] });
    await expect(SkillInstaller.installRemote(project, { version: `${version}-rc.1` }))
      .rejects.toThrow('Invalid stable skill version');
  });

  it('falls back to a shallow exact-tag clone only after npx fails', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') throw new Error('npx unavailable');
      return defaultRunner(command, args, cwd);
    });
    const result = await SkillInstaller.installRemote(project, { version: 'v2.1.0' });
    expect(result.acquisition).toBe('git');
    const clone = runner.mock.calls.find(([command, args]) => command === 'git' && args[0] === 'clone');
    expect(clone?.[1]).toEqual(expect.arrayContaining(['--depth', '1', '--branch', 'v2.1.0', '--single-branch']));
  });

  it('fails without destination mutation when both acquisition methods fail', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx' || (command === 'git' && args[0] === 'clone')) throw new Error('offline');
      return defaultRunner(command, args, cwd);
    });
    await expect(SkillInstaller.installRemote(project, { version: '2.1.0' }))
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
    await expect(SkillInstaller.installRemote(project, { version: '2.1.0' }))
      .rejects.toThrow('not the canonical 8-skill set');

    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') {
        await populateNpxStage(cwd!);
        await fs.symlink('/tmp', path.join(cwd!, '.pi/skills/ralph-loop-yylo/escape'));
        return { stdout: '', stderr: '' };
      }
      if (command === 'git' && args[0] === 'clone') throw new Error('fallback disabled');
      return defaultRunner(command, args, cwd);
    });
    await expect(SkillInstaller.installRemote(project, { version: '2.1.0' }))
      .rejects.toThrow('symbolic link');
  });

  it('rejects a seven-skill stage missing benchmark-yylo before any installation', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') {
        await populateNpxStage(cwd!);
        for (const group of GROUPS) {
          await fs.remove(path.join(cwd!, group, 'benchmark-yylo'));
        }
        return { stdout: '', stderr: '' };
      }
      return defaultRunner(command, args, cwd);
    });
    await expect(SkillInstaller.installRemote(project))
      .rejects.toThrow('not the canonical 8-skill set');
    for (const group of GROUPS) expect(await fs.pathExists(path.join(project, group))).toBe(false);
    expect(await SkillInstaller.getInstallRecord(project)).toBeUndefined();
  });

  it('rejects staged skill identity mismatches before any installation', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') {
        await populateNpxStage(cwd!);
        for (const group of GROUPS) {
          await fs.writeFile(path.join(cwd!, group, 'wiki-yylo/SKILL.md'), '---\nname: obsolete-name\n---\n');
        }
        return { stdout: '', stderr: '' };
      }
      return defaultRunner(command, args, cwd);
    });
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('frontmatter identity mismatch');
    expect(await fs.pathExists(path.join(project, '.pi'))).toBe(false);
    expect(await SkillInstaller.getInstallRecord(project)).toBeUndefined();
  });

  it('distinguishes missing, unrecorded and modified installations offline', async () => {
    expect((await SkillInstaller.inspectGuidance(project)).findings).toHaveLength(GROUPS.length * SKILLS.length);
    await SkillInstaller.installRemote(project);
    await fs.remove(path.join(project, '.juno_task/runtime/skills-install.json'));
    runner.mockClear();
    const unrecorded = await SkillInstaller.inspectGuidance(project);
    expect(unrecorded.findings).toHaveLength(GROUPS.length * SKILLS.length);
    expect(unrecorded.findings.every((finding) => finding.reason === 'unverified')).toBe(true);
    expect(await SkillInstaller.needsUpdate(project)).toBe(true);
    expect(runner).not.toHaveBeenCalled();
  });

  it('preflights every conflict before writing or retiring anything', async () => {
    await createRecordedLegacyInstall();
    const conflict = path.join(project, '.pi/skills/understand-project-yylo/SKILL.md');
    await fs.outputFile(conflict, 'owner bytes\n');
    await fs.outputFile(path.join(project, '.agents/skills/unrelated/SKILL.md'), 'unrelated\n');

    await expect(SkillInstaller.installRemote(project, { version: '2.1.0' }))
      .rejects.toThrow('Skill conflict at .pi/skills/understand-project-yylo');
    expect(await fs.pathExists(path.join(project, '.agents/skills/kanban-workflow'))).toBe(true);
    expect(await fs.pathExists(path.join(project, '.agents/skills/artifact-yylo'))).toBe(false);
  });

  it('force replaces only canonical destinations and preserves unrelated skills', async () => {
    const conflict = path.join(project, '.pi/skills/understand-project-yylo/SKILL.md');
    const unrelated = path.join(project, '.agents/skills/unrelated/SKILL.md');
    await fs.outputFile(conflict, 'owner bytes\n');
    await fs.outputFile(unrelated, 'unrelated\n');

    await SkillInstaller.installRemote(project, { version: '2.1.0', force: true });
    expect(await fs.readFile(conflict, 'utf8')).toContain('name: understand-project-yylo');
    expect(await fs.readFile(unrelated, 'utf8')).toBe('unrelated\n');
    expect(await SkillInstaller.getInstallRecord(project)).toMatchObject({ skills: SKILLS });
  });

  it('retires only recorded byte-identical legacy installs', async () => {
    await createRecordedLegacyInstall();
    const result = await SkillInstaller.installRemote(project, { version: '2.1.0' });
    expect(result.warnings).toBeUndefined();
    for (const group of GROUPS) for (const skill of LEGACY) {
      expect(await fs.pathExists(path.join(project, group, skill))).toBe(false);
    }
  });

  it('rolls back new installs and legacy retirement when record publication fails', async () => {
    await createRecordedLegacyInstall();
    vi.spyOn(fs, 'writeJson').mockRejectedValueOnce(new Error('record write failed'));
    await expect(SkillInstaller.installRemote(project, { version: '2.1.0' }))
      .rejects.toThrow('record write failed');
    for (const group of GROUPS) {
      expect(await fs.pathExists(path.join(project, group, 'kanban-workflow/SKILL.md'))).toBe(true);
      expect(await fs.pathExists(path.join(project, group, 'artifact-yylo'))).toBe(false);
    }
    expect(await SkillInstaller.getInstallRecord(project)).toMatchObject({ version: 'v1.0.0' });
  });

  it('preserves customized and unrecorded legacy skills with actionable warnings', async () => {
    await createRecordedLegacyInstall();
    const customized = path.join(project, '.claude/skills/ralph-loop/SKILL.md');
    await fs.appendFile(customized, 'owner customization\n');
    const result = await SkillInstaller.installRemote(project, { version: '2.1.0' });
    expect(await fs.pathExists(customized)).toBe(true);
    expect(result.warnings).toContain('Preserved customized or unrecorded legacy skill at .claude/skills/ralph-loop');

    const second = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-unrecorded-'));
    try {
      await fs.outputFile(path.join(second, '.agents/skills/kanban-workflow/SKILL.md'), 'owner\n');
      const unrecorded = await SkillInstaller.installRemote(second, { version: '2.1.0' });
      expect(unrecorded.warnings).toContain('Preserved customized or unrecorded legacy skill at .agents/skills/kanban-workflow');
      expect(await fs.pathExists(path.join(second, '.agents/skills/kanban-workflow'))).toBe(true);
    } finally {
      await fs.remove(second);
    }
  });

  it('keeps list and status inspection offline', async () => {
    await SkillInstaller.installRemote(project, { version: '2.1.0' });
    runner.mockClear();
    expect(await SkillInstaller.needsUpdate(project)).toBe(false);
    expect((await SkillInstaller.listSkillGroups(project))[0]?.files).toHaveLength(SKILLS.length);
    expect(await SkillInstaller.getInstallRecord(project)).toMatchObject({ version: 'v2.1.0' });
    expect(runner).not.toHaveBeenCalled();
    await fs.writeFile(path.join(project, '.agents/skills/ledger-tasks-yylo/SKILL.md'), 'changed\n');
    expect(await SkillInstaller.needsUpdate(project)).toBe(true);
    expect(runner).not.toHaveBeenCalled();
  });

  it('enforces metadata-controller ignored-surface safety before networking', async () => {
    await fs.writeJson(path.join(project, '.juno_task/config.json'), {
      controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' },
    });
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('requires the reviewed ignored-runtime policy');
    expect(runner).not.toHaveBeenCalled();
  });

  it('exposes the compatibility requirement and preserved legacy guidance in offline CLI status', async () => {
    await SkillInstaller.installRemote(project);
    const receiptPath = path.join(project, '.juno_task/runtime/skills-install.json');
    const receipt = await fs.readJson(receiptPath);
    receipt.version = 'v2.0.1';
    await fs.writeJson(receiptPath, receipt);
    await fs.outputFile(path.join(project, '.pi/skills/ralph-loop/references/implement.md'), 'yy merge drive');
    vi.spyOn(process, 'cwd').mockReturnValue(project);
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    runner.mockClear();
    await createSkillsCommand().parseAsync(['status'], { from: 'user' });
    const output = log.mock.calls.flat().join('\n');
    expect(output).toContain('Required: ^2.1.0');
    expect(output).toContain('incompatible-version');
    expect(output).toContain('retired-lifecycle');
    expect(output).toContain('without --force');
    expect(runner).not.toHaveBeenCalled();
  });

  it.runIf(fs.existsSync(path.resolve('../yylo-skills/VERSION')))('installs the canonical source release identically across all agent destinations', async () => {
    const source = path.resolve('../yylo-skills');
    const version = (await fs.readFile(path.join(source, 'VERSION'), 'utf8')).trim();
    expect(semver.satisfies(version, SkillInstaller.VERSION_RANGE)).toBe(true);
    expect(semver.prerelease(version)).toBeNull();
    await prepareUpgrade();
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'git' && args.includes('ls-remote')) {
        return { stdout: `abc\trefs/tags/v${version}\n`, stderr: '' };
      }
      if (command === 'npx') {
        expect(args).toContain(`https://github.com/yylo-dev/yylo-skills/tree/v${version}`);
        for (const group of GROUPS) {
          await fs.copy(path.join(source, 'skills'), path.join(cwd!, group));
        }
        return { stdout: '', stderr: '' };
      }
      return defaultRunner(command, args, cwd);
    });
    expect(await SkillInstaller.installRemote(project)).toMatchObject({ changed: true, version: `v${version}` });
    const canonical = await fs.readFile(path.join(source, 'skills/ralph-loop-yylo/references/implement.md'));
    expect(canonical.toString()).toContain('Optional read-only');
    expect(canonical.toString()).toContain('Finish independently enforces admission');
    for (const group of GROUPS) {
      expect(await fs.readFile(path.join(project, group, 'ralph-loop-yylo/references/implement.md'))).toEqual(canonical);
    }
    expect(await SkillInstaller.inspectGuidance(project)).toMatchObject({ coherent: true, version: `v${version}` });
    const raw = 'Record ##{actual-slug} literal $(touch /tmp/not-executed) $1';
    for (const skill of SKILLS) {
      const discovered = findSkillFile(skill, project);
      expect(discovered).not.toBeNull();
      expect(await fs.readFile(discovered!, 'utf8')).toEqual(
        await fs.readFile(path.join(source, 'skills', skill, 'SKILL.md'), 'utf8'));
      const expanded = expandSkillInvocation(`/skill:${skill} ${raw}`, project);
      expect(expanded).toContain(raw);
      expect(expanded).toContain('actual immutable Record ID');
      expect(expanded).toContain('actual Ledger slug');
      expect(expanded).toContain('Record kind/profile');
    }
    // The actual Pi destination remains discoverable without Claude's copy.
    await fs.remove(path.join(project, '.claude/skills'));
    expect(findSkillFile('wiki-yylo', project)).toBe(path.join(project, '.pi/skills/wiki-yylo/SKILL.md'));
  });

  it('requires the release declared by the CLI and rejects old or incompatible exact versions offline', async () => {
    expect(SkillInstaller.VERSION_RANGE).toBe('^2.1.0');
    for (const version of ['2.0.1', '2.0.2', '2.0.3', '2.0.4', '2.0.9', '1.0.0', '3.0.0']) {
      await expect(SkillInstaller.installRemote(project, { version })).rejects.toThrow('is incompatible');
    }
    expect(runner).not.toHaveBeenCalled();
    expect(await fs.pathExists(path.join(project, '.pi'))).toBe(false);
  });

  it('selects the latest compatible stable tag, excluding future majors and prereleases', async () => {
    runner.mockImplementation(async (command, args, cwd) => {
      if (args.includes('ls-remote')) return {
        stdout: ['2.0.3', '2.0.4', '2.1.0', '3.0.0', '2.2.0-rc.1']
          .map((version) => `abc\trefs/tags/v${version}`).join('\n'), stderr: '',
      };
      return defaultRunner(command, args, cwd);
    });
    expect((await SkillInstaller.installRemote(project)).version).toBe('v2.1.0');
  });

  it('fails before acquisition when the required release has not been published', async () => {
    runner.mockResolvedValue({ stdout: 'abc\trefs/tags/v2.0.1\n', stderr: '' });
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('No compatible stable');
    expect(runner).toHaveBeenCalledTimes(1);
    expect(await fs.pathExists(path.join(project, '.pi'))).toBe(false);
  });

  it('reports old but byte-coherent receipts as outdated without network calls', async () => {
    await SkillInstaller.installRemote(project);
    const receiptPath = path.join(project, '.juno_task/runtime/skills-install.json');
    const receipt = await fs.readJson(receiptPath);
    receipt.version = 'v2.0.1';
    await fs.writeJson(receiptPath, receipt);
    const before = await fs.readFile(receiptPath);
    runner.mockClear();
    expect(await SkillInstaller.needsUpdate(project)).toBe(true);
    expect(await SkillInstaller.inspectGuidance(project)).toMatchObject({
      coherent: false,
      findings: [{ destination: '.juno_task/runtime/skills-install.json', reason: 'incompatible-version' }],
    });
    expect(await fs.readFile(receiptPath)).toEqual(before);
    expect(runner).not.toHaveBeenCalled();
  });

  const prepareUpgrade = async () => {
    await SkillInstaller.installRemote(project);
    const receiptPath = path.join(project, '.juno_task/runtime/skills-install.json');
    const receipt = await fs.readJson(receiptPath);
    receipt.version = 'v2.0.1';
    await fs.writeJson(receiptPath, receipt);
    runner.mockImplementation(async (command, args, cwd) => {
      if (command === 'npx') {
        await populateNpxStage(cwd!, 'new native guidance');
        return { stdout: '', stderr: '' };
      }
      return defaultRunner(command, args, cwd);
    });
    return { receiptPath, before: await fs.readFile(receiptPath) };
  };

  it('upgrades unchanged receipt-owned skills across all agents without force', async () => {
    await prepareUpgrade();
    const unrelated = path.join(project, '.pi/skills/project-owned/SKILL.md');
    await fs.outputFile(unrelated, 'owner bytes');
    expect((await SkillInstaller.installRemote(project)).changed).toBe(true);
    for (const group of GROUPS) for (const skill of SKILLS) {
      expect(await fs.readFile(path.join(project, group, skill, 'SKILL.md'), 'utf8')).toContain('new native guidance');
    }
    expect(await SkillInstaller.needsUpdate(project)).toBe(false);
    expect(await fs.readFile(unrelated, 'utf8')).toBe('owner bytes');
    expect((await SkillInstaller.installRemote(project)).changed).toBe(false);
  });

  it('preserves every old copy and receipt when one managed destination is customized', async () => {
    const { receiptPath, before } = await prepareUpgrade();
    const customized = path.join(project, '.pi/skills/wiki-yylo/SKILL.md');
    await fs.appendFile(customized, 'owner edit');
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('Skill conflict');
    expect(await fs.readFile(customized, 'utf8')).toContain('owner edit');
    expect(await fs.readFile(path.join(project, '.agents/skills/wiki-yylo/SKILL.md'), 'utf8')).toContain('canonical $ARGUMENTS');
    expect(await fs.readFile(receiptPath)).toEqual(before);
  });

  it.each(['missing', 'malformed', 'foreign'])('does not infer upgrade ownership from a %s receipt', async (kind) => {
    const { receiptPath } = await prepareUpgrade();
    if (kind === 'missing') await fs.remove(receiptPath);
    else if (kind === 'malformed') await fs.writeFile(receiptPath, '{bad');
    else {
      const receipt = await fs.readJson(receiptPath);
      receipt.repository = 'https://example.invalid/skills';
      await fs.writeJson(receiptPath, receipt);
    }
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('Skill conflict');
    expect(await fs.readFile(path.join(project, '.pi/skills/wiki-yylo/SKILL.md'), 'utf8')).toContain('canonical $ARGUMENTS');
  });

  it.each(['destination', 'receipt'])('refuses a stale %s after staging without overwriting concurrent bytes', async (kind) => {
    const { receiptPath } = await prepareUpgrade();
    const destination = path.join(project, '.pi/skills/wiki-yylo/SKILL.md');
    const copy = fs.copy.bind(fs);
    let changed = false;
    vi.spyOn(fs, 'copy').mockImplementation(async (source, target, options) => {
      await copy(source, target, options);
      if (!changed && String(target).includes('.yylo-stage-')) {
        changed = true;
        await fs.writeFile(kind === 'receipt' ? receiptPath : destination, 'concurrent owner bytes');
      }
    });
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('changed during staging');
    expect(await fs.readFile(kind === 'receipt' ? receiptPath : destination, 'utf8')).toBe('concurrent owner bytes');
    expect(await fs.readFile(path.join(project, '.agents/skills/wiki-yylo/SKILL.md'), 'utf8')).toContain('canonical $ARGUMENTS');
  });

  it('restores all old skill bytes and receipt if upgrade publication fails', async () => {
    const { receiptPath, before } = await prepareUpgrade();
    vi.spyOn(fs, 'writeJson').mockRejectedValueOnce(new Error('record write failed'));
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('record write failed');
    for (const group of GROUPS) for (const skill of SKILLS) {
      expect(await fs.readFile(path.join(project, group, skill, 'SKILL.md'), 'utf8')).toContain('canonical $ARGUMENTS');
    }
    expect(await fs.readFile(receiptPath)).toEqual(before);
  });

  it('restores the current backup if activating a prepared upgrade fails', async () => {
    const { receiptPath, before } = await prepareUpgrade();
    const rename = fs.rename.bind(fs);
    vi.spyOn(fs, 'rename').mockImplementation(async (source, destination) => {
      if (String(source).includes('.yylo-stage-') && String(destination).endsWith('/wiki-yylo')) {
        throw new Error('activation failed');
      }
      return rename(source, destination);
    });
    await expect(SkillInstaller.installRemote(project)).rejects.toThrow('activation failed');
    for (const group of GROUPS) for (const skill of SKILLS) {
      expect(await fs.readFile(path.join(project, group, skill, 'SKILL.md'), 'utf8')).toContain('canonical $ARGUMENTS');
    }
    expect(await fs.readFile(receiptPath)).toEqual(before);
  });

  it('creates Pi settings once and preserves user settings', async () => {
    await SkillInstaller.installRemote(project, { version: '2.1.0' });
    expect(await fs.readJson(path.join(project, '.pi/settings.json')))
      .toEqual({ skills: ['.claude/skills'], quietStartup: true });
    const custom = { theme: 'dark', skills: ['/owner/skills'] };
    await fs.writeJson(path.join(project, '.pi/settings.json'), custom);
    await SkillInstaller.installRemote(project, { version: '2.1.0', force: true });
    expect(await fs.readJson(path.join(project, '.pi/settings.json'))).toEqual(custom);
  });
});
