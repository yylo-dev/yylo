import fs from 'fs-extra';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

describe('YYLO release documentation identity', () => {
  it('uses only the scoped RC package in the launch post', async () => {
    const launchPost = await fs.readFile(path.resolve('docs/hackernews_post.md'), 'utf8');
    expect(launchPost).toContain('npm install -g @yylo/cli@0.1.0-rc.1');
    expect(launchPost).toContain('https://www.npmjs.com/package/%40yylo%2Fcli');
    expect(launchPost).not.toMatch(/npm install -g (?:juno-code|yylo)(?:\s|`|$)/m);
    expect(launchPost).not.toContain('https://www.npmjs.com/package/juno-code');
    expect(launchPost).not.toMatch(/juno[-_ ]code/i);
  });

  it('keeps active package docs, hooks, help, and logo on YYLO identity', async () => {
    const [readme, acceptance, security, hook, merge] = await Promise.all([
      fs.readFile(path.resolve('README.md'), 'utf8'),
      fs.readFile(path.resolve('docs/bolt-package-acceptance.md'), 'utf8'),
      fs.readFile(path.resolve('docs/security_scan.md'), 'utf8'),
      fs.readFile(path.resolve('hooks/check-file-sizes.sh'), 'utf8'),
      fs.readFile(path.resolve('src/cli/commands/merge.ts'), 'utf8'),
    ]);

    expect([acceptance, hook, merge].join('\n')).not.toMatch(/juno[-_ ](?:code|ledger)/i);
    expect(readme).toContain('pypi.org/project/yylo-ledger');
    expect(readme).toContain("pip install 'yylo-ledger==0.3.0'");
    expect(readme).toContain('exact Ledger compatibility policy `0.3.0`');
    expect(readme).not.toContain('pypi.org/project/juno-ledger');
    expect(readme).not.toContain('yy release train');
    expect(security).toContain('`YYLO_*` prefix for application settings');
    // The publishable artwork moved under assets/ (aYaq4g); accept the
    // canonical shipped logo path instead of the removed root icon (OEeK82).
    expect(await fs.pathExists(path.resolve('assets/yylo-logo-square-neon-green.png'))).toBe(true);
    expect(await fs.pathExists(path.resolve('Juno-code-icon.png'))).toBe(false);
  });
});
