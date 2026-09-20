import fs from 'fs-extra';
import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import * as os from 'node:os';
import * as path from 'node:path';
import { promisify } from 'node:util';
import semver from 'semver';
import packageMetadata from '../../package.json';
import { assertSafeManagedWritePath, lstatIfPresent } from './managed-update-transaction.js';

interface SkillGroup {
  name: string;
  destDir: string;
}

interface CommandResult {
  stdout: string;
  stderr: string;
}

interface InstallRecord {
  schemaVersion: 1;
  repository: string;
  version: string;
  acquisition: 'npx' | 'git';
  skills: string[];
  digests: Record<string, Record<string, string>>;
  installedAt: string;
}

export interface SkillInstallResult {
  changed: boolean;
  version: string;
  acquisition: 'npx' | 'git';
  warnings?: string[];
}

export interface SkillGuidanceReport {
  coherent: boolean;
  version: string | null;
  findings: Array<{
    destination: string;
    reason: 'legacy-skill' | 'retired-lifecycle' | 'receipt-drift' | 'unverified' | 'unsafe-or-unreadable' | 'incompatible-version' | 'missing';
  }>;
}

const execFileAsync = promisify(execFile);

export class SkillInstaller {
  static readonly VERSION_RANGE = packageMetadata.yyloSkills.version;

  private static compatibleVersion(version: unknown): version is string {
    return typeof version === 'string' && Boolean(semver.valid(version))
      && semver.satisfies(version, this.VERSION_RANGE);
  }

  private static recordedDigest(record: InstallRecord | undefined, group: string, skill: string): string | undefined {
    // Old releases may prove ownership for upgrades/retirement, not compatibility.
    if (record?.schemaVersion !== 1 || record.repository !== this.REPOSITORY
        || typeof record.version !== 'string' || !semver.valid(record.version)
        || !Array.isArray(record.skills) || !record.skills.includes(skill)) return undefined;
    const digest = record.digests?.[group]?.[skill];
    return typeof digest === 'string' && /^[a-f0-9]{64}$/.test(digest) ? digest : undefined;
  }

  static readonly REPOSITORY = 'https://github.com/yylo-dev/yylo-skills.git';
  static readonly SKILLS = [
    'artifact-yylo',
    'ledger-tasks-yylo',
    'plan-ledger-tasks-yylo',
    'ralph-loop-yylo',
    'understand-project-yylo',
    'wiki-yylo',
    'workflow-yylo',
  ] as const;

  private static readonly LEGACY_SKILLS = [
    'kanban-workflow',
    'plan-kanban-tasks',
    'ralph-loop',
    'understand-project',
  ] as const;

  private static readonly CONTROLLER_AGENT_IGNORES = [
    '/AGENTS.md',
    '/CLAUDE.md',
    '/.agents/',
    '/.claude/',
    '/.pi/',
  ];

  private static readonly SKILL_GROUPS: SkillGroup[] = [
    { name: 'codex', destDir: '.agents/skills' },
    { name: 'claude', destDir: '.claude/skills' },
    { name: 'pi', destDir: '.pi/skills' },
  ];

  private static readonly DEFAULT_PI_SETTINGS = {
    skills: ['.claude/skills'],
    quietStartup: true,
  };

  private static async runCommand(
    command: string,
    args: string[],
    cwd?: string,
  ): Promise<CommandResult> {
    return execFileAsync(command, args, {
      cwd,
      encoding: 'utf8',
      timeout: 120_000,
      maxBuffer: 4 * 1024 * 1024,
      env: { ...process.env, CI: '1', NO_COLOR: '1' },
    });
  }

  static async isMetadataOnlyController(projectDir: string): Promise<boolean> {
    try {
      const config = await fs.readJson(path.join(projectDir, '.juno_task/config.json'));
      return config?.controllerWorkspace?.mode === 'metadata-only'
        && config.controllerWorkspace.policy === '.juno_task/config/metadata-controller.json';
    } catch {
      return false;
    }
  }

  static async assertInstallAllowed(projectDir: string): Promise<void> {
    if (!(await this.isMetadataOnlyController(projectDir))) return;
    const ignorePath = path.join(projectDir, '.gitignore');
    const lines = new Set(
      (await fs.readFile(ignorePath, 'utf8').catch(() => ''))
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean),
    );
    const missing = this.CONTROLLER_AGENT_IGNORES.filter((entry) => !lines.has(entry));
    if (missing.length > 0) {
      throw new Error(
        `Metadata-controller agent surface requires the reviewed ignored-runtime policy; missing .gitignore entries: ${missing.join(', ')}`,
      );
    }
    let tracked = '';
    try {
      ({ stdout: tracked } = await this.runCommand(
        'git',
        ['-c', 'core.fsmonitor=false', 'ls-files', '-z', '--', 'AGENTS.md', 'CLAUDE.md', '.agents', '.claude', '.pi'],
        projectDir,
      ));
    } catch (error) {
      throw new Error(`Metadata-controller agent surface tracking preflight failed: ${String(error)}`);
    }
    const trackedPaths = tracked.split('\0').filter(Boolean).sort();
    if (trackedPaths.length > 0) {
      throw new Error(
        `Metadata-controller agent surface contains tracked user evidence; reviewed evacuation is required: ${trackedPaths.join(', ')}`,
      );
    }
  }

  private static async resolveVersion(requested?: string): Promise<string> {
    const normalized = requested?.trim();
    if (normalized) {
      const withoutPrefix = normalized.startsWith('v') ? normalized.slice(1) : normalized;
      if (!semver.valid(withoutPrefix) || semver.prerelease(withoutPrefix)) {
        throw new Error(`Invalid stable skill version: ${requested}`);
      }
      if (!this.compatibleVersion(withoutPrefix)) {
        throw new Error(`Skill release ${requested} is incompatible; this CLI requires yylo-skills ${this.VERSION_RANGE}. Run yy skills install for the latest compatible stable release.`);
      }
      const tag = `v${withoutPrefix}`;
      const { stdout } = await this.runCommand(
        'git',
        ['ls-remote', '--tags', this.REPOSITORY, `refs/tags/${tag}`, `refs/tags/${tag}^{}`],
      );
      if (!stdout.trim()) throw new Error(`Skill version does not exist: ${tag}`);
      return tag;
    }

    const { stdout } = await this.runCommand(
      'git',
      ['ls-remote', '--tags', '--refs', this.REPOSITORY, 'refs/tags/v*'],
    );
    const versions = stdout
      .split(/\r?\n/)
      .map((line) => line.match(/refs\/tags\/v([^\s]+)$/)?.[1])
      .filter((value): value is string => this.compatibleVersion(value));
    const latest = semver.rsort(versions)[0];
    if (!latest) throw new Error(`No compatible stable yylo-skills release is available; this CLI requires ${this.VERSION_RANGE}. A maintainer must publish the required skill release before this CLI is released.`);
    return `v${latest}`;
  }

  private static async acquireWithNpx(stage: string, version: string): Promise<void> {
    const source = `https://github.com/yylo-dev/yylo-skills/tree/${version}`;
    await this.runCommand(
      'npx',
      [
        '--yes',
        'skills',
        'add',
        source,
        '--skill',
        ...this.SKILLS,
        '--agent',
        'codex',
        'claude-code',
        'pi',
        '--copy',
        '--yes',
      ],
      stage,
    );
  }

  private static async acquireWithGit(stage: string, version: string): Promise<void> {
    const clone = path.join(stage, 'repository');
    await this.runCommand(
      'git',
      ['clone', '--depth', '1', '--branch', version, '--single-branch', this.REPOSITORY, clone],
      stage,
    );
    for (const group of this.SKILL_GROUPS) {
      const root = path.join(stage, group.destDir);
      await fs.ensureDir(root);
      for (const skill of this.SKILLS) {
        await fs.copy(path.join(clone, 'skills', skill), path.join(root, skill), {
          overwrite: false,
          dereference: false,
          preserveTimestamps: false,
        });
      }
    }
  }

  private static async walkFiles(root: string): Promise<string[]> {
    const files: string[] = [];
    const walk = async (current: string, prefix: string): Promise<void> => {
      const entries = await fs.readdir(current, { withFileTypes: true });
      for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
        if (entry.name === '.' || entry.name === '..') throw new Error('Invalid staged skill path');
        const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
        const absolute = path.join(current, entry.name);
        const stat = await fs.lstat(absolute);
        if (stat.isSymbolicLink()) throw new Error(`Staged skill contains a symbolic link: ${relative}`);
        if (stat.isDirectory()) await walk(absolute, relative);
        else if (stat.isFile()) files.push(relative);
        else throw new Error(`Staged skill contains an unsupported entry: ${relative}`);
      }
    };
    await walk(root, '');
    return files;
  }

  private static async directoryDigest(root: string): Promise<string> {
    const hash = createHash('sha256');
    for (const relative of await this.walkFiles(root)) {
      const absolute = path.join(root, relative);
      hash.update(relative);
      hash.update('\0');
      hash.update(String((await fs.stat(absolute)).mode & 0o777));
      hash.update('\0');
      hash.update(await fs.readFile(absolute));
      hash.update('\0');
    }
    return hash.digest('hex');
  }

  private static async validateStage(stage: string): Promise<void> {
    let canonical: Map<string, string> | undefined;
    for (const group of this.SKILL_GROUPS) {
      const root = path.join(stage, group.destDir);
      const entries = await fs.readdir(root, { withFileTypes: true });
      const names = entries.map((entry) => entry.name).sort();
      if (entries.some((entry) => !entry.isDirectory())
          || names.join('\0') !== [...this.SKILLS].sort().join('\0')) {
        throw new Error(`Staged ${group.name} skills are not the canonical seven-skill set`);
      }
      const digests = new Map<string, string>();
      for (const skill of this.SKILLS) {
        const skillRoot = path.join(root, skill);
        const files = await this.walkFiles(skillRoot);
        if (!files.includes('SKILL.md')) throw new Error(`Staged skill is missing SKILL.md: ${skill}`);
        const text = await fs.readFile(path.join(skillRoot, 'SKILL.md'), 'utf8');
        const frontmatter = text.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/)?.[1];
        if (!frontmatter?.split(/\r?\n/).includes(`name: ${skill}`)) {
          throw new Error(`Staged skill frontmatter identity mismatch: ${skill}`);
        }
        digests.set(skill, await this.directoryDigest(skillRoot));
      }
      if (!canonical) canonical = digests;
      else {
        for (const skill of this.SKILLS) {
          if (digests.get(skill) !== canonical.get(skill)) {
            throw new Error(`Staged agent copies differ for skill: ${skill}`);
          }
        }
      }
    }
  }

  private static recordPath(projectDir: string): string {
    return path.join(projectDir, '.juno_task', 'runtime', 'skills-install.json');
  }

  private static async installStage(
    projectDir: string,
    stage: string,
    record: InstallRecord,
    force: boolean,
  ): Promise<{ changed: boolean; warnings: string[] }> {
    const replacements: { source: string; destination: string; backup: string; prepared: string; preimage: string | undefined }[] = [];
    const retirements: { destination: string; backup: string; preimage: string }[] = [];
    const warnings: string[] = [];
    const transaction = randomUUID();
    const recordPath = this.recordPath(projectDir);
    await assertSafeManagedWritePath(projectDir, recordPath);
    const previousRecordBytes = await fs.readFile(recordPath).catch(() => undefined);
    let previousRecord: InstallRecord | undefined;
    try {
      previousRecord = previousRecordBytes
        ? JSON.parse(previousRecordBytes.toString('utf8')) as InstallRecord
        : undefined;
    } catch {
      // An invalid record grants no retirement authority.
    }

    for (const group of this.SKILL_GROUPS) {
      for (const skill of this.SKILLS) {
        const source = path.join(stage, group.destDir, skill);
        const destination = path.join(projectDir, group.destDir, skill);
        await assertSafeManagedWritePath(projectDir, destination);
        const exists = await fs.pathExists(destination);
        const current = exists ? await this.directoryDigest(destination) : undefined;
        const same = exists && (await this.directoryDigest(source)) === current;
        const owned = current !== undefined
          && current === this.recordedDigest(previousRecord, group.name, skill);
        if (exists && !same && !owned && !force) {
          throw new Error(`Skill conflict at ${path.relative(projectDir, destination)}; preserve customized or unrecorded bytes for owner review. Do not use --force without explicit replacement authority`);
        }
        if (!exists || !same || force) {
          replacements.push({
            source,
            destination,
            backup: `${destination}.yylo-backup-${transaction}`,
            prepared: `${destination}.yylo-stage-${transaction}`,
            preimage: current,
          });
        }
      }

      for (const legacy of this.LEGACY_SKILLS) {
        const destination = path.join(projectDir, group.destDir, legacy);
        if (!(await fs.pathExists(destination))) continue;
        await assertSafeManagedWritePath(projectDir, destination);
        const expected = this.recordedDigest(previousRecord, group.name, legacy);
        let current: string | undefined;
        try {
          current = await this.directoryDigest(destination);
        } catch {
          // Unsafe or unreadable legacy content is preserved.
        }
        if (expected && current === expected) {
          retirements.push({ destination, backup: `${destination}.yylo-retired-${transaction}`, preimage: expected });
        } else {
          warnings.push(`Preserved customized or unrecorded legacy skill at ${path.relative(projectDir, destination)}`);
        }
      }
    }

    const applied: typeof replacements = [];
    const retired: typeof retirements = [];
    let recordWriteAttempted = false;
    try {
      for (const item of replacements) {
        await fs.ensureDir(path.dirname(item.destination));
        await fs.copy(item.source, item.prepared, { overwrite: false, dereference: false });
      }
      // Staging may take time. Refuse stale ownership evidence before replacing
      // any destination, even when force was explicitly requested.
      await assertSafeManagedWritePath(projectDir, recordPath);
      const currentRecordBytes = await fs.readFile(recordPath).catch(() => undefined);
      if (previousRecordBytes
        ? !currentRecordBytes || !previousRecordBytes.equals(currentRecordBytes)
        : currentRecordBytes !== undefined) {
        throw new Error('Skill installation receipt changed during staging; inspect and retry');
      }
      for (const item of [...replacements, ...retirements]) {
        await assertSafeManagedWritePath(projectDir, item.destination);
        const current = await fs.pathExists(item.destination)
          ? await this.directoryDigest(item.destination) : undefined;
        if (current !== item.preimage) {
          throw new Error(`Skill destination changed during staging: ${path.relative(projectDir, item.destination)}; inspect and retry`);
        }
      }
      for (const item of replacements) {
        if (await fs.pathExists(item.destination)) await fs.rename(item.destination, item.backup);
        try {
          await fs.rename(item.prepared, item.destination);
        } catch (error) {
          if (await fs.pathExists(item.backup)) await fs.rename(item.backup, item.destination);
          throw error;
        }
        applied.push(item);
      }
      for (const item of retirements) {
        await fs.rename(item.destination, item.backup);
        retired.push(item);
      }
      await fs.ensureDir(path.dirname(recordPath));
      recordWriteAttempted = true;
      await fs.writeJson(recordPath, record, { spaces: 2 });
    } catch (error) {
      for (const item of [...retired].reverse()) {
        if (await fs.pathExists(item.backup)) await fs.rename(item.backup, item.destination);
      }
      for (const item of [...applied].reverse()) {
        await fs.remove(item.destination).catch(() => undefined);
        if (await fs.pathExists(item.backup)) await fs.rename(item.backup, item.destination);
      }
      if (recordWriteAttempted) {
        if (previousRecordBytes) await fs.outputFile(recordPath, previousRecordBytes);
        else await fs.remove(recordPath).catch(() => undefined);
      }
      throw error;
    } finally {
      for (const item of replacements) {
        await fs.remove(item.prepared).catch(() => undefined);
      }
      // Backups survive any failed rollback; never destroy the only old bytes.
    }
    for (const item of [...replacements, ...retirements]) {
      await fs.remove(item.backup).catch(() => {
        warnings.push(`Preserved transaction backup at ${path.relative(projectDir, item.backup)}`);
      });
    }
    return { changed: replacements.length > 0 || retirements.length > 0, warnings };
  }

  static async installRemote(
    projectDir: string,
    options: { force?: boolean; version?: string; silent?: boolean } = {},
  ): Promise<SkillInstallResult> {
    await this.assertInstallAllowed(projectDir);
    const version = await this.resolveVersion(options.version);
    const stage = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-skills-'));
    let acquisition: 'npx' | 'git' = 'npx';
    try {
      try {
        await this.acquireWithNpx(stage, version);
      } catch (npxError) {
        acquisition = 'git';
        await fs.emptyDir(stage);
        try {
          await this.acquireWithGit(stage, version);
        } catch (gitError) {
          throw new Error(`Skill acquisition failed with npx (${String(npxError)}) and Git (${String(gitError)})`);
        }
      }
      await this.validateStage(stage);
      const digests: Record<string, Record<string, string>> = {};
      for (const group of this.SKILL_GROUPS) {
        digests[group.name] = {};
        for (const skill of this.SKILLS) {
          digests[group.name]![skill] = await this.directoryDigest(
            path.join(stage, group.destDir, skill),
          );
        }
      }
      const installation = await this.installStage(
        projectDir,
        stage,
        {
          schemaVersion: 1,
          repository: this.REPOSITORY,
          version,
          acquisition,
          skills: [...this.SKILLS],
          digests,
          installedAt: new Date().toISOString(),
        },
        Boolean(options.force),
      );
      await this.ensurePiSettings(projectDir, options.silent ?? true);
      if (!options.silent) console.log(`✓ Installed YYLO skills ${version} via ${acquisition}`);
      return {
        changed: installation.changed,
        version,
        acquisition,
        ...(installation.warnings.length > 0 ? { warnings: installation.warnings } : {}),
      };
    } finally {
      await fs.remove(stage);
    }
  }

  static async install(
    projectDir: string,
    silent = false,
    force = false,
    _strict = false,
    version?: string,
  ): Promise<boolean> {
    return (await this.installRemote(projectDir, {
      silent,
      force,
      ...(version ? { version } : {}),
    })).changed;
  }

  /** Read-only guidance inspection, independent of CLI package/version receipts.
   * Only the supported skill names are inspected; project skills and historical
   * task/wiki evidence are not reinterpreted as active execution instructions.
   */
  static async inspectGuidance(projectDir: string): Promise<SkillGuidanceReport> {
    const findings: SkillGuidanceReport['findings'] = [];
    let record: InstallRecord | undefined;
    const receipt = this.recordPath(projectDir);
    try {
      await assertSafeManagedWritePath(projectDir, receipt);
      const receiptStat = await lstatIfPresent(receipt);
      if (receiptStat) {
        if (!receiptStat.isFile() || receiptStat.size > 1024 * 1024) throw new Error('Unsafe skill receipt');
        record = await fs.readJson(receipt);
        if (record?.schemaVersion !== 1 || record.repository !== this.REPOSITORY ||
            !Array.isArray(record.skills) || record.skills.length !== this.SKILLS.length ||
            !this.SKILLS.every((skill) => record?.skills.includes(skill)) ||
            typeof record.version !== 'string' || !semver.valid(record.version)) {
          record = undefined;
          findings.push({ destination: path.relative(projectDir, receipt), reason: 'unverified' });
        }
      }
    } catch {
      findings.push({ destination: path.relative(projectDir, receipt), reason: 'unsafe-or-unreadable' });
    }
    if (record && !this.compatibleVersion(record.version)) {
      findings.push({ destination: path.relative(projectDir, receipt), reason: 'incompatible-version' });
    }
    for (const group of this.SKILL_GROUPS) {
      for (const skill of [...this.SKILLS, ...this.LEGACY_SKILLS]) {
        const destination = `${group.destDir}/${skill}`;
        const root = path.join(projectDir, destination);
        try {
          await assertSafeManagedWritePath(projectDir, root);
          if (!(await lstatIfPresent(root))) {
            if ((this.SKILLS as readonly string[]).includes(skill)) {
              findings.push({ destination, reason: record?.skills.includes(skill) ? 'receipt-drift' : 'missing' });
            }
            continue;
          }
          const legacy = (this.LEGACY_SKILLS as readonly string[]).includes(skill);
          if (legacy) findings.push({ destination, reason: 'legacy-skill' });
          // Bounded, no-symlink traversal. Hash bytes/modes exactly like the
          // independent skill receipt, including nested non-Markdown resources.
          const files: string[] = [];
          let count = 0;
          let bytes = 0;
          const walk = async (directory: string, depth: number): Promise<void> => {
            if (depth > 12) throw new Error('Skill inspection depth exceeded');
            await assertSafeManagedWritePath(projectDir, directory);
            const entries = await fs.readdir(directory, { withFileTypes: true });
            for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
              if (++count > 1024 || entry.isSymbolicLink()) throw new Error('Unsafe or oversized skill');
              const absolute = path.join(directory, entry.name);
              if (entry.isDirectory()) await walk(absolute, depth + 1);
              else if (entry.isFile()) files.push(path.relative(root, absolute).split(path.sep).join('/'));
              else throw new Error('Unsupported skill entry');
            }
          };
          await walk(root, 0);
          const hash = createHash('sha256');
          for (const relative of files) {
            const absolute = path.join(root, relative);
            await assertSafeManagedWritePath(projectDir, absolute);
            const stat = await fs.lstat(absolute);
            bytes += stat.size;
            if (!stat.isFile() || stat.size > 1024 * 1024 || bytes > 8 * 1024 * 1024) {
              throw new Error('Unsafe or oversized skill file');
            }
            const content = await fs.readFile(absolute);
            hash.update(relative); hash.update('\0');
            hash.update(String(stat.mode & 0o777)); hash.update('\0');
            hash.update(content); hash.update('\0');
            const text = relative.toLowerCase().endsWith('.md') ? content.toString().replace(/\s+/g, ' ') : '';
            if (
              /\byy(?:lo)?\s+merge\s+(?:arbiter|drive|next|resolve)\b/i.test(text) ||
              /Reviewer A then Reviewer B|low risk has zero semantic reviews|normal risk has at most one/i.test(text)
            ) {
              findings.push({ destination: `${destination}/${relative}`, reason: 'retired-lifecycle' });
            }
          }
          if (!legacy) {
            const expected = record?.skills.includes(skill) ? record.digests?.[group.name]?.[skill] : undefined;
            if (!expected) findings.push({ destination, reason: 'unverified' });
            else if (!files.includes('SKILL.md') || hash.digest('hex') !== expected) {
              findings.push({ destination, reason: 'receipt-drift' });
            }
          }
        } catch {
          findings.push({ destination, reason: 'unsafe-or-unreadable' });
        }
      }
    }
    return { coherent: findings.length === 0, version: record?.version ?? null, findings };
  }

  /** Local-only status check. This method never resolves tags or invokes a command. */
  static async needsUpdate(projectDir: string): Promise<boolean> {
    const record = await fs.readJson(this.recordPath(projectDir)).catch(() => undefined) as InstallRecord | undefined;
    if (!record || record.schemaVersion !== 1 || record.repository !== this.REPOSITORY
        || !this.compatibleVersion(record.version) || !Array.isArray(record.skills)
        || record.skills.length !== this.SKILLS.length
        || !this.SKILLS.every((skill) => record.skills.includes(skill))) return true;
    for (const group of this.SKILL_GROUPS) {
      for (const skill of this.SKILLS) {
        const root = path.join(projectDir, group.destDir, skill);
        if (!(await fs.pathExists(path.join(root, 'SKILL.md')))) return true;
        try {
          const expected = record.digests?.[group.name]?.[skill];
          if (!expected || await this.directoryDigest(root) !== expected) return true;
        } catch {
          return true;
        }
      }
    }
    return false;
  }

  /** Local-only listing. */
  static async listSkillGroups(projectDir: string): Promise<
    { name: string; destDir: string; files: { name: string; installed: boolean; upToDate: boolean }[] }[]
  > {
    const recordCurrent = !(await this.needsUpdate(projectDir));
    const results = [];
    for (const group of this.SKILL_GROUPS) {
      const files = [];
      for (const skill of this.SKILLS) {
        const installed = await fs.pathExists(path.join(projectDir, group.destDir, skill, 'SKILL.md'));
        files.push({ name: skill, installed, upToDate: installed && recordCurrent });
      }
      results.push({ name: group.name, destDir: group.destDir, files });
    }
    return results;
  }

  static async getInstallRecord(projectDir: string): Promise<InstallRecord | undefined> {
    return fs.readJson(this.recordPath(projectDir)).catch(() => undefined) as Promise<InstallRecord | undefined>;
  }

  private static isLegacyGeneratedPiSettings(settings: unknown): settings is { skills: string[] } {
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) return false;
    const object = settings as Record<string, unknown>;
    const keys = Object.keys(object);
    return keys.length === 1
      && keys[0] === 'skills'
      && Array.isArray(object.skills)
      && object.skills.length === 1
      && object.skills[0] === '.claude/skills';
  }

  static async ensurePiSettings(projectDir: string, silent = true): Promise<void> {
    const settingsPath = path.join(projectDir, '.pi', 'settings.json');
    if (await fs.pathExists(settingsPath)) {
      try {
        const existing = JSON.parse(await fs.readFile(settingsPath, 'utf8')) as unknown;
        if (this.isLegacyGeneratedPiSettings(existing)) {
          await fs.writeFile(
            settingsPath,
            `${JSON.stringify({ ...existing, quietStartup: true }, null, 2)}\n`,
          );
          if (!silent) console.log('✓ Updated .pi/settings.json');
        }
      } catch {
        // Preserve malformed or user-owned settings without mutation.
      }
      return;
    }
    await fs.ensureDir(path.dirname(settingsPath));
    await fs.writeFile(settingsPath, `${JSON.stringify(this.DEFAULT_PI_SETTINGS, null, 2)}\n`);
    if (!silent) console.log('✓ Created .pi/settings.json');
  }

  static getSkillGroups(): SkillGroup[] {
    return this.SKILL_GROUPS.map((group) => ({ ...group }));
  }
}
