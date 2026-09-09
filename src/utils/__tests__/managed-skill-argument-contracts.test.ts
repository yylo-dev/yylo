import { execFileSync } from 'node:child_process';
import fs from 'fs-extra';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const project = process.cwd();
const sourceRoot = path.join(project, 'src/templates/skills');

describe('remote skill package boundary', () => {
  it('passes the metadata-only skill contract lint', () => {
    expect(() =>
      execFileSync(process.execPath, ['scripts/verify-skill-argument-contracts.mjs'], {
        cwd: project,
        stdio: 'pipe',
      }),
    ).not.toThrow();
  });

  it('does not retain bundled SKILL.md payloads or a skill-copy build step', async () => {
    const payloads: string[] = [];
    const walk = async (dir: string): Promise<void> => {
      for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
        const absolute = path.join(dir, entry.name);
        if (entry.isDirectory()) await walk(absolute);
        else if (entry.name === 'SKILL.md') payloads.push(path.relative(project, absolute));
      }
    };
    await walk(sourceRoot);
    expect(payloads).toEqual([]);
    const packageJson = await fs.readJson(path.join(project, 'package.json'));
    expect(packageJson.scripts['build:copy-skills']).toBeUndefined();
    expect(packageJson.scripts.build).not.toContain('copy-skills');
    const cli = await fs.readFile(path.join(project, 'src/bin/cli.ts'), 'utf8');
    expect(cli).not.toContain('SkillInstaller.autoUpdate');
    expect(cli).not.toContain('SkillInstaller.install(');
    expect(cli).not.toContain('SkillInstaller.preflightInstall');
  });

  it('retains only argument metadata for the canonical four skills', async () => {
    const contract = await fs.readJson(path.join(sourceRoot, 'argument-contracts.json'));
    expect(Object.keys(contract.skills).sort()).toEqual([
      'kanban-workflow',
      'plan-kanban-tasks',
      'ralph-loop',
      'understand-project',
    ]);
    expect(contract.skills['ralph-loop'].placeholders).toEqual({ $ARGUMENTS: 1 });
    expect(contract.skills['understand-project'].placeholders).toEqual({
      $1: 1,
      $2: 1,
      $ARGUMENTS: 1,
    });
  });
});
