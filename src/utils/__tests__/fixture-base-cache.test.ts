import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import {
  createColdFixture,
  createFixtureOverlay,
  defaultFixtureBaseRoot,
  ensureFixtureBase,
  FIXTURE_BASE_DISABLE_ENV,
  FIXTURE_BASE_SCHEMA,
  fixtureBaseKey,
  fixtureIdentityForRepository,
  makeFixtureTreeOwnerWritable,
  type FixtureBaseKeyInput,
  type SealedFixtureBase,
} from '../../test-utils/fixture-base-cache.js';

const REPOSITORY = path.resolve(import.meta.dirname, '../../../..');

/**
 * Wave 1 fixture-base cache contract tests (7djT8N):
 * key stability and drift, immutability, overlay isolation, corruption and
 * stale-claim recovery, concurrent consumers, cleanup safety, and cold
 * fallback. All fixtures live under a private temporary bases root so these
 * tests never touch the developer's shared cache.
 */
describe('content-addressed fixture bases', () => {
  let basesRoot: string;
  let scratch: string;
  const overlays: string[] = [];

  const identity = (overrides: Partial<FixtureBaseKeyInput> = {}): FixtureBaseKeyInput => ({
    fixtureSchema: `${FIXTURE_BASE_SCHEMA}:test`,
    dependencyLockSha256: 'a'.repeat(64),
    admissionContractSha256: 'b'.repeat(64),
    pythonIdentity: 'python3:test-version',
    nodeVersion: process.version,
    ...overrides,
  });

  beforeAll(() => {
    scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-fixture-base-test-'));
    basesRoot = path.join(scratch, 'bases');
  });

  afterAll(() => {
    // Sealed bases are read-only by design; restore owner write permission
    // without ever following virtualenv or fixture symlinks.
    try {
      makeFixtureTreeOwnerWritable(basesRoot);
    } catch {
      // best effort; rm below still attempts removal
    }
    fs.rmSync(scratch, { recursive: true, force: true });
    for (const overlay of overlays) fs.rmSync(overlay, { recursive: true, force: true });
  });

  const ensureBase = (input: FixtureBaseKeyInput): SealedFixtureBase =>
    ensureFixtureBase(fixtureBaseKey(input), input, { basesRoot });

  it('derives stable keys that change when any identity component drifts', () => {
    const base = identity();
    expect(fixtureBaseKey(base)).toBe(fixtureBaseKey(identity()));
    for (const field of [
      'dependencyLockSha256',
      'admissionContractSha256',
      'pythonIdentity',
      'nodeVersion',
    ] as const) {
      const drifted = identity({ [field]: 'drifted' } as Partial<FixtureBaseKeyInput>);
      expect(fixtureBaseKey(drifted)).not.toBe(fixtureBaseKey(base));
    }
    expect(fixtureBaseKey(identity({ fixtureSchema: 'other' }))).not.toBe(fixtureBaseKey(base));
    expect(fixtureBaseKey(identity({ contract: 'task-workspace.v1' }))).not.toBe(fixtureBaseKey(base));
    expect(fixtureBaseKey(identity({ boundInputs: { policy: 'drift' } }))).not.toBe(fixtureBaseKey(base));
  });

  it('materializes once and reuses the exact sealed base across calls', () => {
    const input = identity();
    const first = ensureBase(input);
    const second = ensureBase(input);
    expect(second.root).toBe(first.root);
    expect(fs.existsSync(path.join(first.root, 'controller', '.venv_juno', 'bin'))).toBe(true);
    expect(fs.existsSync(path.join(first.root, 'controller', '.git', 'HEAD'))).toBe(true);
    const manifest = JSON.parse(
      fs.readFileSync(path.join(first.root, 'yylo-fixture-base.json'), 'utf8'),
    ) as { immutable: boolean; key: string; content_sha256: string };
    expect(manifest.immutable).toBe(true);
    expect(manifest.key).toBe(fixtureBaseKey(input));
  });

  it('seals bases so attempted mutation fails at the filesystem level', () => {
    const base = ensureBase(identity({ dependencyLockSha256: 'c'.repeat(64) }));
    const target = path.join(base.root, 'controller', '.juno_task', 'scripts', 'evil.txt');
    expect(() => fs.writeFileSync(target, 'mutation')).toThrow();
    expect(fs.existsSync(target)).toBe(false);
  });

  it('fails closed when a sealed base is corrupted', () => {
    const input = identity({ dependencyLockSha256: 'd'.repeat(64) });
    const base = ensureBase(input);
    // Corrupt despite read-only sealing (chmod as owner is still permitted).
    const head = path.join(base.root, 'controller', '.git', 'HEAD');
    fs.chmodSync(head, 0o644);
    fs.writeFileSync(head, 'ref: refs/heads/mutated\n');
    fs.chmodSync(head, 0o444);
    const next = ensureFixtureBase(fixtureBaseKey(input), input, { basesRoot });
    // The corrupt base is quarantined and a sound base is rebuilt at the key.
    const manifest = JSON.parse(
      fs.readFileSync(path.join(next.root, 'yylo-fixture-base.json'), 'utf8'),
    ) as { content_sha256: string };
    const rebuiltHead = fs.readFileSync(path.join(next.root, 'controller', '.git', 'HEAD'), 'utf8');
    expect(rebuiltHead).toBe('ref: refs/heads/fixture-controller\n');
    expect(manifest.content_sha256).toBeTruthy();
    const quarantined = fs
      .readdirSync(basesRoot)
      .filter((name) => name.includes('.corrupt-'));
    expect(quarantined.length).toBeGreaterThanOrEqual(1);
  });

  it('gives every consumer an isolated writable overlay', () => {
    const base = ensureBase(identity({ dependencyLockSha256: 'e'.repeat(64) }));
    const first = createFixtureOverlay(base, { overlayParent: path.join(scratch, 'overlays') });
    const second = createFixtureOverlay(base, { overlayParent: path.join(scratch, 'overlays') });
    overlays.push(first.root, second.root);
    expect(first.root).not.toBe(second.root);
    const marker = path.join(first.controllerPath, '.juno_task', 'scripts', 'only-first.txt');
    fs.writeFileSync(marker, 'isolated');
    expect(fs.existsSync(path.join(second.controllerPath, '.juno_task', 'scripts', 'only-first.txt'))).toBe(false);
    // Overlays are writable and the base is untouched by overlay mutation.
    expect(() => fs.writeFileSync(marker, 'rewritable')).not.toThrow();
    expect(fs.existsSync(path.join(base.root, 'controller', '.juno_task', 'scripts', 'only-first.txt'))).toBe(false);
    // Overlay venvs stay executable through the copy.
    const python = path.join(first.controllerPath, '.venv_juno', 'bin', 'python');
    expect(fs.existsSync(python)).toBe(true);
    const version = execFileSync(python, ['--version'], { encoding: 'utf8' });
    expect(version).toMatch(/^Python 3/);
    first.release();
    second.release();
    expect(fs.existsSync(first.root)).toBe(false);
    // The shared base survives overlay release.
    expect(fs.existsSync(path.join(base.root, 'yylo-fixture-base.json'))).toBe(true);
  });

  it('never mutates an external symlink target while making cleanup possible', () => {
    const external = path.join(scratch, 'external-python');
    const fixture = path.join(scratch, 'symlink-cleanup-fixture');
    const link = path.join(fixture, 'bin', 'python');
    fs.writeFileSync(external, '#!/bin/sh\necho isolated\n', { mode: 0o755 });
    fs.chmodSync(external, 0o755);
    fs.mkdirSync(path.dirname(link), { recursive: true });
    fs.symlinkSync(external, link);
    fs.chmodSync(path.dirname(link), 0o555);
    fs.chmodSync(fixture, 0o555);

    const snapshot = () => {
      const target = fs.statSync(external);
      const linkEntry = fs.lstatSync(link);
      return {
        targetInode: target.ino,
        targetMode: target.mode & 0o777,
        targetSha256: createHash('sha256').update(fs.readFileSync(external)).digest('hex'),
        linkInode: linkEntry.ino,
        linkType: linkEntry.isSymbolicLink(),
        linkText: fs.readlinkSync(link),
      };
    };
    const before = snapshot();
    makeFixtureTreeOwnerWritable(fixture);
    const afterWritable = snapshot();
    fs.rmSync(fixture, { recursive: true, force: true });

    expect(afterWritable).toEqual(before);
    expect(fs.existsSync(link)).toBe(false);
    expect(fs.readFileSync(external, 'utf8')).toContain('isolated');
    expect(fs.statSync(external).mode & 0o777).toBe(0o755);
  });

  it('makes release interruption retries idempotent', () => {
    const base = ensureBase(identity({ dependencyLockSha256: '1'.repeat(64) }));
    const overlay = createFixtureOverlay(base, { overlayParent: path.join(scratch, 'overlays-retry') });
    const readOnly = path.join(overlay.controllerPath, '.juno_task');
    fs.chmodSync(readOnly, 0o555);
    overlay.release();
    expect(fs.existsSync(overlay.root)).toBe(false);
    expect(() => overlay.release()).not.toThrow();
  });

  it('never deletes foreign entries or escapes through a substituted link', () => {
    const parent = path.join(scratch, 'overlays-foreign');
    fs.mkdirSync(parent, { recursive: true });
    const foreign = path.join(parent, 'foreign-owned');
    fs.mkdirSync(foreign, { recursive: true });
    fs.writeFileSync(path.join(foreign, 'keep.txt'), 'foreign');
    const base = ensureBase(identity({ dependencyLockSha256: 'f'.repeat(64) }));
    const overlay = createFixtureOverlay(base, { overlayParent: parent });
    overlay.release();
    // The foreign sibling survives an overlay release in the same parent.
    expect(fs.existsSync(path.join(foreign, 'keep.txt'))).toBe(true);

    const substituted = createFixtureOverlay(base, { overlayParent: parent });
    fs.rmSync(substituted.root, { recursive: true, force: true });
    fs.symlinkSync(foreign, substituted.root);
    expect(() => substituted.release()).toThrow('refusing to delete foreign overlay path');
    expect(fs.readFileSync(path.join(foreign, 'keep.txt'), 'utf8')).toBe('foreign');
    fs.unlinkSync(substituted.root);
  });

  it('recovers a stale materialization claim left by a dead process', () => {
    const claim = path.join(basesRoot, `yylo-fixture-base.materialize.claim.999999999`);
    fs.writeFileSync(claim, `${Date.now() - 60_000}\n`, { flag: 'wx' });
    const base = ensureBase(identity({ dependencyLockSha256: '0'.repeat(64) }));
    expect(fs.existsSync(path.join(base.root, 'yylo-fixture-base.json'))).toBe(true);
  });

  it('supports the explicit cold fallback', () => {
    expect(process.env[FIXTURE_BASE_DISABLE_ENV]).toBeUndefined();
    const cold = createColdFixture();
    expect(cold.cold).toBe(true);
    expect(cold.baseKey).toBeNull();
    expect(fs.existsSync(path.join(cold.controllerPath, '.venv_juno', 'bin'))).toBe(true);
    cold.release();
    expect(fs.existsSync(cold.root)).toBe(false);
  });

  it('derives the repository identity from real lock and admission manifests', () => {
    const derived = fixtureIdentityForRepository(REPOSITORY);
    expect(derived.dependencyLockSha256).toMatch(/^[0-9a-f]{64}$/);
    expect(derived.admissionContractSha256).toMatch(/^[0-9a-f]{64}$/);
    expect(derived.pythonIdentity).toContain('python3');
    expect(derived.nodeVersion).toBe(process.version);
  });

  it('does not share the developer cache root by default under test overrides', () => {
    // The tests above deliberately use a private root; ensure the default root
    // is namespaced under the real tmpdir and not inside the repository.
    const root = defaultFixtureBaseRoot();
    expect(root.startsWith(fs.realpathSync(os.tmpdir()))).toBe(true);
    expect(root.includes(REPOSITORY)).toBe(false);
  });
});
