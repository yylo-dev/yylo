import { afterEach, describe, expect, it } from 'vitest';
import { execa } from 'execa';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';

const temporary: string[] = [];
afterEach(async () => Promise.all(temporary.splice(0).map((item) => fs.remove(item))));

describe('YYLO launch identity', () => {
  async function migrationEnvironment(env: Record<string, string>) {
    const source = await fs.readFile(path.resolve('src/bin/yylo.sh'), 'utf8');
    const loop = source.match(/while IFS='=' read -r legacy_name _; do[\s\S]*?done < <\(env\)/)?.[0];
    expect(loop).toBeTruthy();
    return execa('bash', ['-c', `set -euo pipefail\n${loop}\nprintf '%s' "$YYLO_MODEL"`], {
      env,
      reject: false,
    });
  }

  it('advertises only the canonical YYLO website in active help', async () => {
    const source = await fs.readFile(path.resolve('src/bin/cli.ts'), 'utf8');
    expect(source).toContain('Website: https://yylo.dev');
    expect(source).not.toContain('Website: https://askbudi.ai');
  });

  it('keeps active release guidance on canonical YYLO coordinates', async () => {
    const [rootReadme, packageReadme, launchPost] = await Promise.all([
      fs.readFile(path.resolve('../README.md'), 'utf8'),
      fs.readFile(path.resolve('README.md'), 'utf8'),
      fs.readFile(path.resolve('docs/hackernews_post.md'), 'utf8'),
    ]);
    expect(rootReadme).toContain('https://www.npmjs.com/package/%40yylo%2Fcli');
    expect(rootReadme).not.toContain('https://www.npmjs.com/package/juno-code');
    expect(rootReadme).toContain('`v0.1.0-rc.1`');
    expect(packageReadme).toContain('`--set v0.1.0-rc.1`');
    // The documented install coordinate advances with releases; assert the
    // canonical scoped package with one concrete exact SemVer (derived from
    // package.json, the active release truth) instead of pinning a frozen RC
    // that drifts on every release commit (OEeK82).
    const metadata = JSON.parse(await fs.readFile(path.resolve('package.json'), 'utf8'));
    expect(packageReadme).toContain(`npm install -g @yylo/cli@${metadata.version}`);
    expect(packageReadme).toContain(`'@yylo/benchmark@${metadata.yyloBenchmark.version}'`);
    expect(packageReadme).toContain(`skills \`${metadata.yyloSkills.version}\``);
    expect(packageReadme).toContain('once published');
    expect(packageReadme).toContain('once separately published');
    expect(packageReadme).toContain('A source bump neither publishes these versions nor activates installed runtimes.');
    expect(packageReadme).not.toMatch(/npm install -g yylo(?:\s|`|$)/m);
    const releaseGuidance = [rootReadme, packageReadme, launchPost].join('\n');
    expect(releaseGuidance).not.toMatch(/npm install -g (?:juno-code|yylo)(?:\s|`|$)/m);
    expect(releaseGuidance).not.toContain('https://www.npmjs.com/package/juno-code');
    expect(packageReadme).not.toContain('release `v2.1.3-rc.1`');
  });

  it('keeps active product, command, hook, and launch-copy identities canonical', async () => {
    const activePublicSurfaces = await Promise.all([
      fs.readFile(path.resolve('docs/hackernews_post.md'), 'utf8'),
      fs.readFile(path.resolve('docs/bolt-package-acceptance.md'), 'utf8'),
      fs.readFile(path.resolve('hooks/check-file-sizes.sh'), 'utf8'),
      fs.readFile(path.resolve('src/templates/services/README.md'), 'utf8'),
      fs.readFile(path.resolve('src/cli/commands/task.ts'), 'utf8'),
      fs.readFile(path.resolve('src/cli/commands/merge.ts'), 'utf8'),
    ]);
    const [rootReadme, packageReadme, cliSource, securityGuidance] = await Promise.all([
      fs.readFile(path.resolve('../README.md'), 'utf8'),
      fs.readFile(path.resolve('README.md'), 'utf8'),
      fs.readFile(path.resolve('src/bin/cli.ts'), 'utf8'),
      fs.readFile(path.resolve('docs/security_scan.md'), 'utf8'),
    ]);

    expect(activePublicSurfaces.join('\n')).not.toMatch(/juno[-_ ](?:code|ledger|benchmark)/i);
    expect(rootReadme).not.toMatch(/Juno (?:Code|Ledger|Benchmark)/);
    expect(packageReadme).not.toMatch(/Juno (?:Code|Ledger|Benchmark)/);
    expect(packageReadme).not.toContain('pypi.org/project/juno-ledger');
    expect(cliSource).not.toContain('Juno Ledger');
    expect(securityGuidance).toContain('`YYLO_*` prefix for application settings');
  });

  it('uses YYLO in onboarding, diagnostics and migration help without renaming compatibility identifiers', async () => {
    const surfaces = [
      'src/cli/commands/workspace.ts', 'src/cli/commands/migrate.ts',
      'src/cli/commands/integration.ts', 'src/cli/commands/test.ts',
      'src/utils/script-installer.ts', 'src/utils/managed-project-assets.ts',
      'src/utils/logger.ts', 'src/core/session-metadata.ts',
      'src/templates/controller-agent/AGENTS.md', 'src/templates/controller-agent/CLAUDE.md',
      'docs/agent-startup.md', 'docs/controller-generation-upgrades.md',
    ];
    for (const file of surfaces) {
      const source = await fs.readFile(path.resolve(file), 'utf8');
      expect(source, file).not.toMatch(/\bJuno\b/);
      expect(source, file).toContain('YYLO');
    }
    const resolver = await fs.readFile(path.resolve('src/templates/scripts/controller_resolver.py'), 'utf8');
    expect(resolver).toContain('JUNO_TASK_ROOT');
    expect(resolver).toContain('juno.controller.path');
    const simple = await fs.readFile(path.resolve('src/utils/simple-init.ts'), 'utf8');
    expect(simple).toContain('.juno_task');
    expect(simple).toContain('juno_task_workspace_state.v1');
    expect(simple).toContain('juno_task_workspace_state.v2');
  });

  it('includes the active YYLO logo in the publishable npm file set', async () => {
    const packed = await execa('npm', ['pack', '--dry-run', '--json', '--ignore-scripts']);
    const report = JSON.parse(packed.stdout) as Array<{ files: Array<{ path: string }> }>;
    expect(report[0]?.files.map((file) => file.path)).toContain('assets/yylo-logo-square-neon-green.png');
  });

  it('maps a legacy-only environment value to the canonical name', async () => {
    const result = await migrationEnvironment({ JUNO_CODE_MODEL: 'legacy' });
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe('legacy');
  });

  it('preserves an explicit canonical value over a conflicting legacy value', async () => {
    const result = await migrationEnvironment({ JUNO_CODE_MODEL: 'legacy', YYLO_MODEL: 'canonical' });
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe('canonical');
  });

  it('refuses a yy collision with a different yylo installation', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'yylo-collision-'));
    temporary.push(root);
    const yy = path.join(root, 'yy');
    const yylo = path.join(root, 'yylo');
    await fs.copy(path.resolve('src/bin/yylo.sh'), yy);
    await fs.writeFile(yylo, '#!/usr/bin/env bash\nexit 0\n');
    await Promise.all([fs.chmod(yy, 0o755), fs.chmod(yylo, 0o755)]);

    const result = await execa(yy, ['--version'], {
      env: { PATH: `${root}:${process.env.PATH ?? ''}` },
      reject: false,
    });

    expect(result.exitCode).toBe(78);
    expect(result.stderr).toContain('yy and yylo resolve to different installations');
    expect(result.stderr).toContain('reinstall @yylo/cli');
  });
});
