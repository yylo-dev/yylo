import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import {
  BUNDLE_COMPONENTS, parseReleasePin, releaseBundleSchema, verifyBundleArtifact,
  verifyBundleDeclarations, verifyPinnedRelease, bundleInputsDigest, verifyBundleAcceptance,
} from '../release-bundle.js';

const digest = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');
function fixture() {
  const payloads = Object.fromEntries(BUNDLE_COMPONENTS.map(role => [role, Buffer.from(`${role} exact artifact`)]));
  const names = { cli: '@yylo/cli', ledger: 'yylo-ledger', benchmark: '@yylo/benchmark', skills: 'yylo-skills' };
  const components = Object.fromEntries(BUNDLE_COMPONENTS.map(role => [role, {
    name: names[role], version: '1.2.3', url: `https://releases.example.invalid/${role}-1.2.3.archive`,
    sha256: digest(payloads[role]), bytes: payloads[role].length,
    source: { repository: `https://example.invalid/${role}`, commit: 'a'.repeat(40) },
  }]));
  const bundle = { schema_version: 'yylo_release_bundle.v1', version: '1.2.3',
    controller_generation: { ordinary_dispatch: 'explicit-only-v1' }, components };
  const bytes = Buffer.from(JSON.stringify(bundle));
  const pin = { schema_version: 'yylo_release_pin.v1', version: '1.2.3', manifest: {
    url: 'https://releases.example.invalid/1.2.3/bundle.json', sha256: digest(bytes),
  } };
  const declarations = {
    cli: { name: '@yylo/cli', version: '1.2.3', yyloControllerGeneration: { ordinaryDispatch: 'explicit-only-v1' }, yyloLedger: { version: '1.2.3' },
      yyloBenchmark: { version: '1.2.3' }, yyloSkills: { version: '^1.2.0' } },
    benchmark: { name: '@yylo/benchmark', version: '1.2.3' },
  };
  return { bundle, bytes, pin, payloads, declarations };
}

describe('portable exact release bundles', () => {
  it('binds all four exact artifacts to externally reviewed pin bytes', () => {
    const f = fixture();
    const pin = parseReleasePin(Buffer.from(JSON.stringify(f.pin)));
    const bundle = verifyPinnedRelease(f.bytes, pin);
    expect(verifyBundleDeclarations(bundle, f.declarations)).toEqual(bundle);
    for (const role of BUNDLE_COMPONENTS) verifyBundleArtifact(bundle, role, f.payloads[role]);
  });

  it.each(BUNDLE_COMPONENTS)('rejects missing, substituted, or modified %s artifacts', role => {
    const f = fixture();
    expect(() => verifyBundleArtifact(f.bundle, role, Buffer.from('wrong'))).toThrow('release_artifact_mismatch');
    const changed = Buffer.from(f.payloads[role]);
    changed[0] ^= 1;
    expect(() => verifyBundleArtifact(f.bundle, role, changed)).toThrow('release_artifact_mismatch');
    delete f.bundle.components[role];
    expect(() => releaseBundleSchema.parse(f.bundle)).toThrow();
  });

  it.each(['^1.2.3', '>=1.2.3', 'latest', 'v1.2.3', '01.2.3', '1.2'])('rejects non-exact component version %s', version => {
    const f = fixture();
    f.bundle.components.skills.version = version;
    expect(() => releaseBundleSchema.parse(f.bundle)).toThrow();
  });

  it.each(['file:///tmp/artifact', 'http://example.invalid/a', 'https://user:secret@example.invalid/a',
    'https://example.invalid/a?token=secret', 'https://example.invalid/a#fragment'])('rejects unsafe portable location %s', url => {
    const f = fixture();
    f.bundle.components.ledger.url = url;
    expect(() => releaseBundleSchema.parse(f.bundle)).toThrow();
    f.pin.manifest.url = url;
    expect(() => parseReleasePin(Buffer.from(JSON.stringify(f.pin)))).toThrow();
  });

  it('rejects unreviewed manifest changes even if version remains identical', () => {
    const f = fixture();
    f.bundle.components.ledger.sha256 = 'b'.repeat(64);
    expect(() => verifyPinnedRelease(Buffer.from(JSON.stringify(f.bundle)), f.pin)).toThrow('release_manifest_digest_mismatch');
  });

  it('rejects pin version mismatch, legacy manifests, unknown keys and local activation fields', () => {
    const f = fixture();
    expect(() => verifyPinnedRelease(f.bytes, { ...f.pin, version: '2.0.0' })).toThrow('release_pin_version_mismatch');
    expect(() => releaseBundleSchema.parse({ ...f.bundle, schema_version: 'yylo_two_package_release.v2' })).toThrow();
    expect(() => releaseBundleSchema.parse({ ...f.bundle, active: true })).toThrow();
    expect(() => releaseBundleSchema.parse({ ...f.bundle, components: { ...f.bundle.components, extra: {} } })).toThrow();
    expect(() => releaseBundleSchema.parse({ ...f.bundle, components: { ...f.bundle.components,
      cli: { ...f.bundle.components.cli, path: '/home/user/install' } } })).toThrow();
    expect(() => parseReleasePin(Buffer.from(JSON.stringify({ ...f.pin, installed: '/tmp/runtime' })))).toThrow();
  });

  it('bounds input and refuses malformed bytes without trusting an embedded checksum', () => {
    const f = fixture();
    expect(() => verifyPinnedRelease(Buffer.alloc(65537), f.pin)).toThrow('release_manifest_too_large');
    expect(() => parseReleasePin(Buffer.alloc(65537))).toThrow('release_manifest_too_large');
    const invalid = Buffer.from([0xff]);
    expect(() => verifyPinnedRelease(invalid, { ...f.pin, manifest: { ...f.pin.manifest, sha256: digest(invalid) } })).toThrow();
    expect(() => parseReleasePin(Buffer.from('{'))).toThrow();
  });

  it('allows independently versioned products but enforces every authored expectation', () => {
    const f = fixture();
    f.bundle.components.skills.version = '1.3.0';
    expect(verifyBundleDeclarations(f.bundle, f.declarations).components.skills.version).toBe('1.3.0');
    f.bundle.components.skills.version = '2.0.0';
    expect(() => verifyBundleDeclarations(f.bundle, f.declarations)).toThrow('release_declaration_mismatch:skills');
  });

  it.each(['cli', 'ledger', 'benchmark'] as const)('refuses stale %s declarations', role => {
    const f = fixture();
    f.bundle.components[role].version = '1.2.4';
    if (role === 'cli') f.bundle.version = '1.2.4';
    expect(() => verifyBundleDeclarations(f.bundle, f.declarations)).toThrow(`release_declaration_mismatch:${role}`);
  });

  it('binds qualification to all component inputs, clean source and exact gate bytes', () => {
    const f = fixture();
    const report = { schema_version: 'yylo_bundle_upgrade_acceptance.v1', outcome: 'passed',
      coverage: 'four-component-upgrade.v1', bundle_inputs_sha256: bundleInputsDigest(f.bundle),
      source: { sha: 'a'.repeat(40), dirty: false }, gate_sha256: 'b'.repeat(64) };
    const bytes = Buffer.from(JSON.stringify(report));
    const bundle = { ...f.bundle, acceptance: { url: 'https://example.invalid/acceptance.json', sha256: digest(bytes), bytes: bytes.length } };
    verifyBundleAcceptance(bundle, bytes, report.source.sha, report.gate_sha256);
    expect(bundleInputsDigest(bundle)).toBe(bundleInputsDigest(f.bundle));
    expect(() => verifyBundleAcceptance(bundle, bytes, 'c'.repeat(40), report.gate_sha256)).toThrow('bundle_acceptance_inputs_mismatch');
    expect(() => verifyBundleAcceptance(bundle, bytes, report.source.sha, 'd'.repeat(64))).toThrow('bundle_acceptance_inputs_mismatch');
    bundle.components.skills.sha256 = 'f'.repeat(64);
    expect(() => verifyBundleAcceptance(bundle, bytes, report.source.sha, report.gate_sha256)).toThrow('bundle_acceptance_inputs_mismatch');
  });

  it('rejects missing, tampered, dirty or CLI-only acceptance', () => {
    const f = fixture();
    expect(() => verifyBundleAcceptance(f.bundle, Buffer.from('{}'), 'a'.repeat(40), 'b'.repeat(64))).toThrow('bundle_acceptance_missing');
    for (const dirty of [true, false]) {
      const bytes = Buffer.from(JSON.stringify({ schema_version: dirty ? 'yylo_bundle_upgrade_acceptance.v1' : 'yylo_controller_upgrade_acceptance.v1',
        outcome: 'passed', coverage: 'four-component-upgrade.v1', bundle_inputs_sha256: bundleInputsDigest(f.bundle),
        source: { sha: 'a'.repeat(40), dirty }, gate_sha256: 'b'.repeat(64) }));
      const bundle = { ...f.bundle, acceptance: { url: 'https://example.invalid/acceptance.json', sha256: digest(bytes), bytes: bytes.length } };
      expect(() => verifyBundleAcceptance(bundle, bytes, 'a'.repeat(40), 'b'.repeat(64))).toThrow();
      expect(() => verifyBundleAcceptance(bundle, Buffer.from('{}'), 'a'.repeat(40), 'b'.repeat(64))).toThrow('bundle_acceptance_digest_mismatch');
    }
  });

  it('rejects a generation capability not supported by the packed CLI', () => {
    const f = fixture();
    f.declarations.cli.yyloControllerGeneration.ordinaryDispatch = 'automatic-upgrade';
    expect(() => verifyBundleDeclarations(f.bundle, f.declarations)).toThrow('release_generation_capability_mismatch');
  });

  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1])('refuses invalid artifact byte length %s', bytes => {
    const f = fixture();
    f.bundle.components.cli.bytes = bytes;
    expect(() => releaseBundleSchema.parse(f.bundle)).toThrow();
  });
});
