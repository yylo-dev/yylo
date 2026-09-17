import fs from 'fs-extra';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import {
  assertControllerGenerationReady, prepareControllerGeneration, applyControllerGeneration,
  recoverControllerGeneration, GENERATION_MIGRATION_ROOT, discoverInstalledGeneration, retainInstalledGeneration,
  type InstalledGenerationEvidence, type ControllerGenerationPlan,
} from './controller-generation-migration.js';
import { assertSafeManagedWritePath } from './managed-update-transaction.js';

export type GenerationAssessment =
  | { disposition: 'ready'; controller: string }
  | { disposition: 'retained'; controller: string; executable: string; reason: string }
  | { disposition: 'migration_required'; controller: string; plan: ControllerGenerationPlan }
  | { disposition: 'transition_incomplete'; controller: string; transactionId: string }
  | { disposition: 'refused'; controller: string; code: string; detail: string; safeNextAction: string };

export const INSTALLED_GENERATION_EVIDENCE = '.yylo-generation-evidence.json';
const hash = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex');

/** Evidence is recorded by authenticated installation, never invented from version equality. */
async function installedEvidence(root: string): Promise<InstalledGenerationEvidence | undefined> {
  const file = path.join(root, INSTALLED_GENERATION_EVIDENCE);
  await assertSafeManagedWritePath(root, file);
  if (!(await fs.pathExists(file))) {
    // npm's installation receipt plus its content-addressed offline cache is
    // also exact artifact evidence. No network, cache repair, or guessed version.
    const pkg = await safeJson(root, 'package.json');
    if (!pkg || !['@yylo/cli', 'juno-code'].includes(pkg.name)) return undefined;
    const modules = path.resolve(root, ...pkg.name.split('/').map(() => '..'));
    if (path.basename(modules) !== 'node_modules') return undefined;
    const lock = await safeJson(modules, '.package-lock.json');
    const entry = lock?.packages?.[`node_modules/${pkg.name}`];
    const cache = path.resolve(process.env.npm_config_cache || path.join(os.homedir(), '.npm'));
    if (!lock) return (await discoverInstalledGeneration(root, cache)).evidence ?? undefined;
    if (!entry || entry.version !== pkg.version || !/^sha512-[A-Za-z0-9+/]+={0,2}$/.test(entry.integrity ?? '')) return undefined;
    const digest = Buffer.from(entry.integrity.slice(7), 'base64').toString('hex');
    if (digest.length !== 128) return undefined;
    const artifact = path.join(cache, '_cacache/content-v2/sha512', digest.slice(0, 2), digest.slice(2, 4), digest.slice(4));
    await assertSafeManagedWritePath(cache, artifact);
    if (!(await fs.pathExists(artifact))) return undefined;
    const bytes = await fs.readFile(artifact);
    if (createHash('sha512').update(bytes).digest('hex') !== digest) throw new Error('package_provenance_invalid: npm artifact integrity mismatch');
    return { root, artifact, sha256: hash(bytes) };
  }
  const value = await fs.readJson(file) as InstalledGenerationEvidence;
  if (Object.keys(value).sort().join(',') !== 'artifact,root,sha256'
      || value.root !== root || !path.isAbsolute(value.artifact)
      || !/^[a-f0-9]{64}$/.test(value.sha256)) {
    throw new Error('package_provenance_invalid: invalid installed generation evidence');
  }
  return value;
}

async function safeJson(controller: string, relative: string): Promise<any> {
  const file = path.join(controller, relative);
  await assertSafeManagedWritePath(controller, file);
  return await fs.pathExists(file) ? fs.readJson(file) : undefined;
}

/** Check an already-bound package without asking permission for a hypothetical update. */
async function exactCurrentPackage(controller: string, packageRoot: string): Promise<boolean> {
  try { return await verifyCurrentPackage(controller, packageRoot); } catch { return false; }
}
async function verifyCurrentPackage(controller: string, packageRoot: string): Promise<boolean> {
  const current = await safeJson(controller, `${GENERATION_MIGRATION_ROOT}/current.json`);
  if (current?.candidate?.root === packageRoot) {
    const plan = await prepareControllerGeneration(controller, current.candidate, current.candidate);
    const before = plan.before as Record<string, unknown>;
    const after = plan.after as Record<string, unknown>;
    return Object.keys(after).every(name => name === `${GENERATION_MIGRATION_ROOT}/current.json`
      || JSON.stringify(before[name]) === JSON.stringify(after[name]));
  }
  await assertSafeManagedWritePath(packageRoot, path.join(packageRoot, 'dist/bin/cli.mjs'));
  await assertSafeManagedWritePath(packageRoot, path.join(packageRoot, 'package.json'));
  const identity = await safeJson(controller, '.juno_task/runtime/identity.json');
  const executable = path.join(packageRoot, 'dist/bin/cli.mjs');
  if (!identity || identity.executable !== executable || identity.source !== 'installed-release'
      || identity.tracked !== false || identity.package !== '@yylo/cli'
      || !(await fs.pathExists(executable))) return false;
  const pkg = await fs.readJson(path.join(packageRoot, 'package.json'));
  if (pkg.name !== identity.package || pkg.version !== identity.version
      || hash(await fs.readFile(executable)) !== identity.executable_sha256) return false;
  const { validateManifest } = await import('./managed-project-assets.js');
  const inventory = validateManifest(await safeJson(controller, '.juno_task/managed-assets.json'), 'controller inventory');
  if (inventory.packageName !== identity.package || inventory.packageVersion !== identity.version) return false;
  for (const [relative, record] of Object.entries(inventory.assets)) {
    if (relative.includes('\\') || path.isAbsolute(relative)
        || relative.split('/').some(part => !part || part === '..' || part === '.')) return false;
    const destination = path.join(controller, relative);
    await assertSafeManagedWritePath(controller, destination);
    if (!record || !(await fs.pathExists(destination))
        || hash(await fs.readFile(destination)) !== record.installedSha256) return false;
  }
  const generation = await safeJson(controller, '.juno_task/runtime/managed-controller/generation.json');
  if (!generation || generation.schema_version !== 'juno_managed_controller_runtime.v1'
      || !generation.scripts || typeof generation.scripts !== 'object'
      || !Object.keys(generation.scripts).length) return false;
  for (const [relative, untyped] of Object.entries(generation.scripts)) {
    const binding = untyped as { source_sha256?: string; actual_sha256?: string; classification?: string };
    if (!relative.startsWith('.juno_task/scripts/') || relative.includes('\\')
        || relative.split('/').some(part => !part || part === '..' || part === '.')) return false;
    const destination = path.join(controller, relative);
    await assertSafeManagedWritePath(controller, destination);
    const source = path.join(packageRoot, 'dist/templates', relative.slice('.juno_task/'.length));
    if (binding.classification !== 'exact' || binding.source_sha256 !== binding.actual_sha256
        || !/^[a-f0-9]{64}$/.test(binding.source_sha256 ?? '')
        || !(await fs.pathExists(source)) || !(await fs.pathExists(destination))
        || hash(await fs.readFile(source)) !== binding.source_sha256
        || hash(await fs.readFile(destination)) !== binding.actual_sha256) return false;
  }
  return true;
}

/** Pure assessment: doctor and first-use dispatch consume the identical decision. */
export async function assessControllerGeneration(controller: string, packageRoot: string): Promise<GenerationAssessment> {
  controller = path.resolve(controller);
  packageRoot = path.resolve(packageRoot);
  try {
    const fence = await safeJson(controller, `${GENERATION_MIGRATION_ROOT}/fence.json`);
    if (fence) {
      if (fence.schema_version !== 'yylo_controller_generation_transaction.v1' || !/^[a-f0-9]{64}$/.test(fence.id)) {
        throw new Error('generation_transition_invalid: preserve the malformed fence');
      }
      return { disposition: 'transition_incomplete', controller, transactionId: fence.id };
    }
    await assertControllerGenerationReady(controller);
    const current = await safeJson(controller, `${GENERATION_MIGRATION_ROOT}/current.json`);
    let candidate = await installedEvidence(packageRoot);
    const identity = await safeJson(controller, '.juno_task/runtime/identity.json');
    const previousRoot = typeof identity?.executable === 'string'
      ? path.resolve(path.dirname(identity.executable), '../..') : undefined;
    const previous: InstalledGenerationEvidence | undefined = current?.candidate
      ?? (previousRoot ? await installedEvidence(previousRoot) : undefined);
    if (candidate && previous) {
      let plan: ControllerGenerationPlan;
      try { plan = await prepareControllerGeneration(controller, candidate, previous); }
      catch (error) {
        if (previousRoot && previousRoot !== packageRoot && await exactCurrentPackage(controller, previousRoot)) {
          return { disposition: 'retained', controller, executable: identity.executable,
            reason: error instanceof Error ? error.message : 'migration unavailable' };
        }
        throw error;
      }
      // The public npm -g path may differ from its immutable retained copy.
      // The first plan above authenticates the invoked package too; only then
      // canonicalize identical artifact bytes to the retained generation.
      if (current && candidate.sha256 === previous.sha256 && candidate.root !== previous.root) {
        candidate = previous;
        plan = await prepareControllerGeneration(controller, candidate, previous);
      }
      // A completed generation still gets engine-authenticated operational assessment.
      if (current && candidate.root === previous.root && candidate.artifact === previous.artifact
          && candidate.sha256 === previous.sha256
          && Object.entries(plan.before as Record<string, unknown>)
            .every(([name, value]) => name === `${GENERATION_MIGRATION_ROOT}/current.json`
              || JSON.stringify(value) === JSON.stringify((plan.after as Record<string, unknown>)[name]))) {
        return { disposition: 'ready', controller };
      }
      return { disposition: 'migration_required', controller, plan };
    }
    if (await exactCurrentPackage(controller, packageRoot)) return { disposition: 'ready', controller };
    if (previousRoot && previousRoot !== packageRoot && await exactCurrentPackage(controller, previousRoot)) {
      return { disposition: 'retained', controller, executable: identity.executable,
        reason: 'candidate generation provenance unavailable; retained admitted runtime' };
    }
    throw new Error('generation_provenance_required: candidate and retained previous installed artifact evidence are required');
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'Generation assessment failed';
    return { disposition: 'refused', controller, code: detail.split(':', 1)[0] ?? 'generation_invalid', detail,
      safeNextAction: 'Preserve controller bytes and prior runtime. Inspect yy scripts generation doctor; install authenticated side-by-side packages with retained artifact evidence before migration. Do not copy scripts or retarget the project.' };
  }
}

/** Explicit reviewed recovery only; never selected by automatic assessment. */
export async function prepareInstalledControllerRepair(controller: string, packageRoot: string): Promise<ControllerGenerationPlan> {
  const candidate = await installedEvidence(path.resolve(packageRoot));
  const identity = await safeJson(controller, '.juno_task/runtime/identity.json');
  const previousRoot = typeof identity?.executable === 'string'
    ? path.resolve(path.dirname(identity.executable), '../..') : undefined;
  const previous = previousRoot ? await installedEvidence(previousRoot) : undefined;
  if (!candidate || !previous) throw new Error('generation_provenance_required: authenticated candidate and previous artifacts required');
  // Validate the mixed predecessor before any external preparation. The
  // controller stays unchanged; the reviewed plan must name the retained
  // executable already, never silently substitute it during repair-apply.
  await prepareControllerGeneration(controller, candidate, previous, true);
  const retained = await retainInstalledGeneration(controller, candidate,
    path.resolve(process.env.npm_config_cache || path.join(os.homedir(), '.npm')),
    path.resolve(process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local/state')));
  return prepareControllerGeneration(controller, retained.evidence, previous, true);
}

/** No prompt and no network. Only the engine can authenticate and mutate the write set. */
export async function ensureControllerGeneration(controller: string, packageRoot: string): Promise<GenerationAssessment> {
  let assessment = await assessControllerGeneration(controller, packageRoot);
  if (assessment.disposition === 'transition_incomplete') {
    await recoverControllerGeneration(controller, assessment.transactionId);
    assessment = await assessControllerGeneration(controller, packageRoot);
  }
  if (assessment.disposition === 'migration_required') {
    const retained = await retainInstalledGeneration(controller, assessment.plan.candidate,
      path.resolve(process.env.npm_config_cache || path.join(os.homedir(), '.npm')),
      path.resolve(process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local/state')));
    // Retention is outside the controller. Reassess all live inputs before the
    // fenced apply rather than changing the authenticated plan in place.
    assessment.plan = await prepareControllerGeneration(controller, retained.evidence, assessment.plan.previous);
    try {
      await applyControllerGeneration(controller, assessment.plan);
    } catch (error) {
      // Roll back only our own fenced attempt, never someone else's concurrent migration.
      const fence = await safeJson(controller, `${GENERATION_MIGRATION_ROOT}/fence.json`);
      if (fence?.id === assessment.plan.id) {
        try { await recoverControllerGeneration(controller, assessment.plan.id, true); }
        catch (rollback) { throw new Error(`generation_recovery_required: migration and rollback failed; preserve journal ${assessment.plan.id}`, { cause: rollback }); }
      }
      const previous = assessment.plan.previous;
      if (previous?.root && await exactCurrentPackage(controller, previous.root)) {
        return { disposition: 'retained', controller,
          executable: path.join(previous.root, 'dist/bin/cli.mjs'),
          reason: 'migration failed; previous coherent generation preserved' };
      }
      throw error;
    }
    await assertControllerGenerationReady(controller);
    return { disposition: 'ready', controller: path.resolve(controller) };
  }
  if (assessment.disposition === 'refused') throw new Error(`${assessment.detail}; ${assessment.safeNextAction}`);
  return assessment;
}
