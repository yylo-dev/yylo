import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';

describe('explicit controller upgrade release/finish gate contract', () => {
  it('ships migration guidance and exposes the offline packed gate', () => {
    const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
    expect(pkg.scripts['test:controller-upgrade']).toBe('node scripts/verify-controller-upgrade.mjs');
    expect(pkg.files).toContain('docs/controller-generation-upgrades.md');
    const guidance = fs.readFileSync('docs/controller-generation-upgrades.md', 'utf8');
    expect(guidance).toContain('representative generated fixture');
    expect(guidance).toContain('before task finish');
    expect(guidance.replace(/\s+/g, ' ')).toContain('prompt or network installation');
  });

  it('requires exact artifact-bound evidence in maintainer preparation and readback', () => {
    execFileSync('bash', [path.resolve('../scripts/tests/release-cli.test.sh')],
      { timeout: 30_000, maxBuffer: 1024 * 1024 });
    const release = fs.readFileSync('../scripts/release-cli.sh', 'utf8');
    expect(release).toContain('yylo_two_package_release.v2');
    expect(release).toContain('verify-controller-upgrade.mjs --artifact "$cli_artifact"');
    expect(release).toContain('r.artifact.sha256!==m.packages.cli.artifact.sha256');
  });
});
