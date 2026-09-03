/**
 * Content-addressed immutable fixture bases with disposable per-run overlays.
 *
 * Wave 1 of the trusted-test performance PDR (7djT8N): replace per-invocation
 * Python-venv and minimum real-Git controller construction with immutable
 * bases keyed by the full fixture identity, plus a private overlay per run.
 *
 * Invariants enforced here:
 *  - The base key is a SHA-256 over the dependency lock, fixture schema,
 *    implementation/admission contract digests, and runtime generation, so any
 *    drift materializes a new base and stale bases are never reused.
 *  - Bases are published by atomic rename and sealed read-only; attempted
 *    mutation of a sealed base fails at the operating-system level and digest
 *    verification fails closed on corruption.
 *  - Every consumer receives its own disposable overlay copied from the base;
 *    overlay cleanup is path-scoped to directories this process created and
 *    can never remove the shared base, a foreign cache entry, or an in-use
 *    overlay owned by another process.
 *  - Concurrent base materialization is serialized through an advisory lock so
 *    two fresh processes cooperate instead of racing.
 *  - A cold fallback reconstructs the fixture directly when the cache root is
 *    unusable, preserving pre-cache behavior.
 */
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import * as path from 'node:path';

export const FIXTURE_BASE_SCHEMA = 'juno.test.fixture.base.v1';
export const FIXTURE_BASE_ROOT_ENV = 'YYLO_TEST_FIXTURE_BASE_ROOT';
export const FIXTURE_BASE_DISABLE_ENV = 'YYLO_TEST_DISABLE_FIXTURE_BASE_CACHE';

export interface FixtureBaseIdentity {
  /** sha256 of the dependency lock governing the measured tree (juno-code). */
  dependencyLockSha256: string;
  /** sha256 over the frozen admission/implementation contract manifests. */
  admissionContractSha256: string;
  /** Interpreter identity (resolved executable + version). */
  pythonIdentity: string;
  /** Node runtime generation (process.version). */
  nodeVersion: string;
}

export interface FixtureBaseKeyInput extends FixtureBaseIdentity {
  fixtureSchema: string;
}

export interface SealedFixtureBase {
  key: string;
  root: string;
  manifest: Record<string, unknown>;
}

export interface FixtureOverlay {
  root: string;
  /** Exact paths created by and owned by this overlay invocation. */
  controllerPath: string;
  baseKey: string | null;
  cold: boolean;
  release: () => void;
}

function sha256Buffer(data: Buffer): string {
  return createHash('sha256').update(data).digest('hex');
}

function sha256File(target: string): string | null {
  try {
    return sha256Buffer(fs.readFileSync(target));
  } catch {
    return null;
  }
}

function fileIdentity(target: string): string | null {
  const digest = sha256File(target);
  return digest;
}

/** Stable digest over the concatenation of ordered file digests. */
function digestOfFiles(files: Array<string | null>): string {
  return sha256Buffer(Buffer.from(files.map((value) => value ?? '-').join('\n'), 'utf8'));
}

export function resolvePythonIdentity(): string {
  const executable = execFileSync('sh', ['-c', 'command -v python3'], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  }).trim();
  const version = execFileSync(executable, ['--version'], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  }).trim();
  return `${executable}:${version}`;
}

export function fixtureBaseKey(input: FixtureBaseKeyInput): string {
  const canonical = JSON.stringify(
    {
      schema: FIXTURE_BASE_SCHEMA,
      fixture_schema: input.fixtureSchema,
      dependency_lock_sha256: input.dependencyLockSha256,
      admission_contract_sha256: input.admissionContractSha256,
      python_identity: input.pythonIdentity,
      node_version: input.nodeVersion,
    },
    null,
    0,
  );
  return sha256Buffer(Buffer.from(canonical, 'utf8'));
}

export function defaultFixtureBaseRoot(): string {
  return path.join(fs.realpathSync(os.tmpdir()), 'yylo-fixture-bases');
}

function entryPath(entry: { parentPath?: string; path?: string; name: string }): string {
  // recursive readdir: parentPath exists on Node 20.12+; older Node 20
  // releases expose the same value as `path`.
  return path.join(entry.parentPath ?? entry.path ?? '.', entry.name);
}

function chmodFixtureEntry(target: string, writable: boolean): void {
  const entry = fs.lstatSync(target);
  // chmod follows symlinks on supported Node platforms. Fixture virtualenvs
  // contain links to interpreters outside the owned tree, so links must never
  // reach chmod (or recursion) here.
  if (entry.isSymbolicLink()) return;
  if (entry.isDirectory()) {
    fs.chmodSync(target, writable ? entry.mode | 0o700 : entry.mode & 0o555);
  } else if (entry.isFile()) {
    // Add/remove only owner write bits. In particular, retain executable bits
    // on copied virtualenv interpreters and scripts.
    fs.chmodSync(target, writable ? entry.mode | 0o600 : entry.mode & 0o555);
  }
}

function sealReadOnly(root: string): void {
  for (const entry of fs.readdirSync(root, { withFileTypes: true, recursive: true })) {
    chmodFixtureEntry(entryPath(entry), false);
  }
  chmodFixtureEntry(root, false);
}

/** Restore owner write permission only inside an owned fixture tree. */
export function makeFixtureTreeOwnerWritable(root: string): void {
  const rootEntry = fs.lstatSync(root);
  if (!rootEntry.isDirectory() || rootEntry.isSymbolicLink()) {
    throw new Error(`refusing to make non-directory fixture root writable: ${root}`);
  }
  for (const entry of fs.readdirSync(root, { withFileTypes: true, recursive: true })) {
    chmodFixtureEntry(entryPath(entry), true);
  }
  chmodFixtureEntry(root, true);
}

function baseManifestDigest(root: string): string {
  return digestOfFiles([
    fileIdentity(path.join(root, 'pyvenv.cfg')),
    fileIdentity(path.join(root, 'controller', '.git', 'HEAD')),
  ]);
}

function writeManifest(root: string, key: string, identity: FixtureBaseKeyInput): void {
  const manifest = {
    schema_version: FIXTURE_BASE_SCHEMA,
    key,
    immutable: true,
    created_at: new Date().toISOString(),
    identity: {
      fixture_schema: identity.fixtureSchema,
      dependency_lock_sha256: identity.dependencyLockSha256,
      admission_contract_sha256: identity.admissionContractSha256,
      python_identity: identity.pythonIdentity,
      node_version: identity.nodeVersion,
    },
    content_sha256: baseManifestDigest(root),
  };
  const manifestPath = path.join(root, 'yylo-fixture-base.json');
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o444 });
  fs.chmodSync(manifestPath, 0o444);
}

function materializeBase(staging: string): void {
  const controller = path.join(staging, 'controller');
  fs.mkdirSync(path.join(controller, '.juno_task', 'scripts'), { recursive: true });
  execFileSync('git', ['init', '-q', '-b', 'fixture-controller', controller], { stdio: 'ignore' });
  const venvRoot = path.join(controller, '.venv_juno');
  execFileSync('python3', ['-m', 'venv', venvRoot], { stdio: 'ignore' });
}

function verifySealedBase(root: string, key: string): Record<string, unknown> {
  const manifestPath = path.join(root, 'yylo-fixture-base.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')) as Record<string, unknown>;
  if (manifest.schema_version !== FIXTURE_BASE_SCHEMA) {
    throw new Error(`fixture base manifest schema mismatch at ${root}`);
  }
  if (manifest.key !== key || manifest.immutable !== true) {
    throw new Error(`fixture base identity mismatch at ${root}`);
  }
  const content = baseManifestDigest(root);
  if (manifest.content_sha256 !== content) {
    throw new Error(`fixture base content digest mismatch at ${root}: expected ${String(manifest.content_sha256)} observed ${content}`);
  }
  return manifest;
}

/** Advisory exclusive lock serializing base materialization for one root. */
function withBaseMaterializationLock<T>(basesRoot: string, action: () => T): T {
  const claim = path.join(basesRoot, `yylo-fixture-base.materialize.claim.${process.pid}`);
  for (let attempt = 0; ; attempt += 1) {
    try {
      fs.writeFileSync(claim, `${Date.now()}\n`, { flag: 'wx' });
      break;
    } catch (error) {
      if (!isClaimExists(error)) throw error;
      // Recover claims whose owning process is gone or older than 10 minutes.
      for (const stale of fs.readdirSync(basesRoot).filter((name) => name.startsWith('yylo-fixture-base.materialize.claim.'))) {
        const stalePath = path.join(basesRoot, stale);
        const pid = Number(stale.split('.').pop());
        let alive = false;
        try {
          process.kill(pid, 0);
          alive = true;
        } catch {
          alive = false;
        }
        const stamp = Number(fs.readFileSync(stalePath, 'utf8').trim() || 0);
        const age = Number.isFinite(stamp) ? Date.now() - stamp : Number.POSITIVE_INFINITY;
        if (!alive || age > 10 * 60 * 1000) {
          try { fs.rmSync(stalePath, { force: true }); } catch { /* raced */ }
        }
      }
      if (attempt > 1200) throw new Error('fixture base materialization lock wait exceeded bound');
      const sleeper = new Int32Array(new SharedArrayBuffer(4));
      Atomics.wait(sleeper, 0, 0, 50);
    }
  }
  try {
    return action();
  } finally {
    try { fs.rmSync(claim, { force: true }); } catch { /* best effort */ }
  }
}

function isClaimExists(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as NodeJS.ErrnoException).code === 'EEXIST';
}

/**
 * Return the sealed immutable base for `key`, materializing it exactly once.
 * Concurrent callers serialize; an existing sealed base is only verified.
 */
export function ensureFixtureBase(
  key: string,
  identity: FixtureBaseKeyInput,
  options: { basesRoot?: string } = {},
): SealedFixtureBase {
  const basesRoot = options.basesRoot ?? defaultFixtureBaseRoot();
  fs.mkdirSync(basesRoot, { recursive: true });
  const root = path.join(basesRoot, key);
  try {
    return { key, root, manifest: verifySealedBase(root, key) };
  } catch {
    // Missing or corrupt: fall through to materialization.
  }
  return withBaseMaterializationLock(basesRoot, () => {
    try {
      return { key, root, manifest: verifySealedBase(root, key) };
    } catch {
      // fall through
    }
    const staging = fs.mkdtempSync(path.join(basesRoot, `staging-${key.slice(0, 8)}-`));
    try {
      materializeBase(staging);
      writeManifest(staging, key, identity);
      sealReadOnly(staging);
      // Verify before publishing; quarantine anything unexpected at the final path.
      verifySealedBase(staging, key);
      try {
        if (fs.existsSync(root)) {
          const quarantine = path.join(basesRoot, `${key}.corrupt-${Date.now()}`);
          fs.chmodSync(root, 0o755);
          fs.renameSync(root, quarantine);
          fs.chmodSync(quarantine, 0o555);
        }
        fs.renameSync(staging, root);
      } catch (error) {
        throw new Error(`fixture base publication failed for ${key}: ${String(error)}`);
      }
      return { key, root, manifest: verifySealedBase(root, key) };
    } finally {
      try { fs.rmSync(staging, { recursive: true, force: true }); } catch { /* already published or cleaned */ }
    }
  });
}

/**
 * Create a private disposable overlay copied from the sealed base. The
 * returned `release` removes exactly this overlay and never touches the base,
 * foreign entries, or overlays owned by other processes.
 */
export function createFixtureOverlay(
  base: SealedFixtureBase,
  options: { overlayParent?: string } = {},
): FixtureOverlay {
  const parent = options.overlayParent ?? path.join(fs.realpathSync(os.tmpdir()), 'yylo-fixture-overlays');
  fs.mkdirSync(parent, { recursive: true });
  const overlayRoot = fs.mkdtempSync(path.join(parent, `run-${process.pid}-`));
  const controllerPath = path.join(overlayRoot, 'controller');
  fs.cpSync(path.join(base.root, 'controller'), controllerPath, {
    recursive: true,
    verbatimSymlinks: true,
    force: true,
  });
  // Overlays are writable copies. Symlinks are skipped and executable bits
  // from the sealed base are retained.
  makeFixtureTreeOwnerWritable(overlayRoot);
  const ownedRoot = fs.realpathSync(overlayRoot);
  const ownedIdentity = fs.lstatSync(overlayRoot);
  const ownedParent = fs.realpathSync(parent);
  return {
    root: overlayRoot,
    controllerPath,
    baseKey: base.key,
    cold: false,
    release: () => {
      // Path-scoped deletion of exactly the directory inode created above.
      // A missing root is a completed/interrupted release retry. A substituted
      // symlink or sibling inode is refused before rm can follow it.
      let current: fs.Stats;
      try {
        current = fs.lstatSync(overlayRoot);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
        throw error;
      }
      if (!current.isDirectory() || current.isSymbolicLink()
          || current.dev !== ownedIdentity.dev || current.ino !== ownedIdentity.ino
          || fs.realpathSync(overlayRoot) !== ownedRoot
          || !ownedRoot.startsWith(`${ownedParent}${path.sep}`)) {
        throw new Error(`refusing to delete foreign overlay path: ${overlayRoot}`);
      }
      makeFixtureTreeOwnerWritable(overlayRoot);
      fs.rmSync(overlayRoot, { recursive: true, force: true });
    },
  };
}

/**
 * Cold fallback: construct the fixture directly with no shared base. Used when
 * the cache is disabled or its root is unusable, preserving pre-cache
 * semantics and timings.
 */
export function createColdFixture(): FixtureOverlay {
  const parent = path.join(fs.realpathSync(os.tmpdir()), 'yylo-suite-');
  const root = fs.mkdtempSync(parent);
  const controllerPath = path.join(root, 'controller');
  fs.mkdirSync(path.join(controllerPath, '.juno_task', 'scripts'), { recursive: true });
  execFileSync('git', ['init', '-q', '-b', 'fixture-controller', controllerPath], { stdio: 'ignore' });
  execFileSync('python3', ['-m', 'venv', path.join(controllerPath, '.venv_juno')], { stdio: 'ignore' });
  return {
    root,
    controllerPath,
    baseKey: null,
    cold: true,
    release: () => {
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

/** Compose the standard Wave-1 identity for the juno-code checkout root. */
export function fixtureIdentityForRepository(repositoryRoot: string): FixtureBaseKeyInput {
  const junoCode = path.join(repositoryRoot, 'juno-code');
  return {
    fixtureSchema: FIXTURE_BASE_SCHEMA,
    dependencyLockSha256: sha256File(path.join(junoCode, 'package-lock.json')) ?? 'absent',
    admissionContractSha256: digestOfFiles([
      fileIdentity(path.join(junoCode, 'scripts', 'implementation-contract.json')),
      fileIdentity(path.join(junoCode, 'src', 'templates', 'managed-assets.json')),
    ]),
    pythonIdentity: resolvePythonIdentity(),
    nodeVersion: process.version,
  };
}
