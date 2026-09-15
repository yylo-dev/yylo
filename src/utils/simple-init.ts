import path from 'node:path';
import fs from 'node:fs/promises';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { SIMPLE_FILES } from '../templates/simple-workspace.js';
import { resolveController } from './controller-resolver.js';

const RESERVATION = '.yylo-simple-init';
const RECEIPT = 'simple-init.json';
const digest = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
const git = (root: string, args: string[]) => execFileSync('git', ['-C', root, ...args], {
  encoding: 'utf8', timeout: 10_000, maxBuffer: 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'],
}).trim();
const gitOptional = (root: string, args: string[]) => {
  const result = spawnSync('git', ['-C', root, ...args], { encoding: 'utf8', timeout: 10_000 });
  if (result.error) throw result.error;
  return result.status === 0 ? result.stdout.trim() : null;
};

async function fileDigest(file: string): Promise<string | null> {
  try {
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`Expected a regular non-symlink file: ${file}`);
    return digest(await fs.readFile(file));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw error;
  }
}

export interface SimpleInitPlan {
  schema: 'yylo_simple_init_plan.v1';
  outcome: 'ready' | 'already-initialized';
  root: string;
  identity: { commonDir: string; head: string | null; ref: string | null; gitConfig: string | null };
  preserved: Record<string, string | null>;
  files: Record<string, string>;
  guidance: string;
}

const generatedFiles = (): Record<string, string> => ({ ...SIMPLE_FILES,
  [RECEIPT]: `${JSON.stringify({ schema: 'yylo_simple_initialization.v1',
    files: Object.fromEntries(Object.entries(SIMPLE_FILES).map(([name, content]) => [name, digest(content)])),
  }, null, 2)}\n`,
});

async function inspectRoot(directory: string): Promise<SimpleInitPlan['identity'] & { root: string }> {
  const root = await fs.realpath(path.resolve(directory));
  let top: string;
  try { top = await fs.realpath(git(root, ['rev-parse', '--show-toplevel'])); }
  catch { throw new Error('Simple initialization requires Git. Run git init explicitly in the intended project folder, then retry; no Git state was created.'); }
  if (top !== root) throw new Error(`Initialize Simple only at the Git top-level root: ${top}`);
  const commonDir = await fs.realpath(git(root, ['rev-parse', '--path-format=absolute', '--git-common-dir']));
  const gitDir = await fs.realpath(git(root, ['rev-parse', '--path-format=absolute', '--git-dir']));
  if (commonDir !== gitDir) throw new Error('Simple initialization refuses linked worktrees. Use a fresh primary Git checkout.');
  const registration = gitOptional(root, ['config', '--local', '--get-regexp', '^juno\\.(controller|workspace|integration)\\.']);
  const worktreeRegistration = gitOptional(root, ['config', '--worktree', '--get-regexp', '^juno\\.(controller|workspace|integration)\\.']);
  if (registration || worktreeRegistration) throw new Error('Managed registration conflicts with Simple initialization; conversion requires a separately authorized fresh-workspace transition.');
  for (const key of ['JUNO_TASK_ROOT', 'JUNO_CONTROL_EFFECTIVE_ROOT', 'JUNO_CONTROL_INVOCATION_ROOT']) {
    const value = process.env[key];
    if (value && path.resolve(root, value) !== root) throw new Error(`${key} assertion mismatch: intended Simple root is ${root}`);
  }
  if (process.env.JUNO_CONTROLLER_BRANCH || ['JUNO_WORKSPACE_ROLE', 'JUNO_CONTROL_INVOCATION_ROLE'].some((key) => process.env[key] && process.env[key] !== 'simple')) {
    throw new Error('Inherited managed workspace authority conflicts with Simple initialization.');
  }
  // Never create a second Simple project nested inside an existing project.
  let ancestor = path.dirname(root);
  while (ancestor !== path.dirname(ancestor)) {
    try {
      await fs.lstat(path.join(ancestor, '.juno_task'));
      throw new Error(`Ancestor Juno workspace conflicts with Simple initialization: ${ancestor}`);
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    ancestor = path.dirname(ancestor);
  }
  return { root, commonDir, head: gitOptional(root, ['rev-parse', '--verify', 'HEAD']),
    ref: gitOptional(root, ['symbolic-ref', '--quiet', 'HEAD']), gitConfig: await fileDigest(path.join(commonDir, 'config')) };
}

/** Pure preflight: no directory creation, package probes, staging or writes. */
export async function planSimpleInit(directory: string): Promise<SimpleInitPlan> {
  const { root, ...identity } = await inspectRoot(directory);
  try {
    await fs.lstat(path.join(root, RESERVATION));
    throw new Error(`Interrupted or active Simple initialization: ${root}/${RESERVATION}. Preserve its bytes and inspect before explicit recovery; no automatic cleanup.`);
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  const preserved: Record<string, string | null> = {};
  for (const name of ['AGENTS.md', 'CLAUDE.md', '.gitignore']) {
    preserved[name] = await fileDigest(path.join(root, name));
    if (name !== '.gitignore' && preserved[name]) {
      const text = await fs.readFile(path.join(root, name), 'utf8');
      if (/yy\s+task\s+(?:start|finish)|metadata[- ]only|integration[- ]owner|mandatory.*worktree/i.test(text)) {
        throw new Error(`Existing ${name} contains managed-lifecycle guidance. Review it explicitly before Simple initialization; the file will not be overwritten.`);
      }
    }
  }
  const files = generatedFiles();
  let outcome: SimpleInitPlan['outcome'] = 'ready';
  const metadata = path.join(root, '.juno_task');
  try {
    const stat = await fs.lstat(metadata);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('Existing .juno_task is not a regular local directory.');
    for (const [name, bytes] of Object.entries(files)) {
      if (await fileDigest(path.join(metadata, name)) !== digest(bytes)) {
        throw new Error(`Existing .juno_task conflicts with fresh initialization (${name}); preserve all bytes. --force is not supported.`);
      }
    }
    const resolution = resolveController(root, 'diagnostic', { trustedResolver: true });
    if (!resolution.valid || resolution.role !== 'simple') throw new Error('Existing workspace is not a valid Simple initialization.');
    outcome = 'already-initialized';
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  const durable = ['config.json', 'simple-agent-guidance.md', RECEIPT, 'tasks/aa/ABC123.md', 'ledger/aa/ABC123/1.json',
    'documents/aa/ABC123/1.json', 'document-ledger/aa/ABC123/1.json', 'artifacts/aa/ABC123/1.json',
    'artifact-ledger/aa/ABC123/1.json', 'objects/sha256/aa/probe', 'archive/probe'];
  for (const name of durable) {
    const result = spawnSync('git', ['-C', root, 'check-ignore', '--no-index', '--', `.juno_task/${name}`], { encoding: 'utf8', timeout: 10_000 });
    if (result.status === 0) throw new Error(`Git ignore rules hide durable Simple data: .juno_task/${name}. Review ignore rules explicitly; they will not be overwritten.`);
    if (result.status !== 1) throw new Error(`Cannot inspect Git ignore rules: ${result.stderr || result.error}`);
  }
  return { schema: 'yylo_simple_init_plan.v1', outcome, root, identity, preserved, files,
    guidance: 'Preserve root AGENTS.md/CLAUDE.md; Simple agents must also read .juno_task/simple-agent-guidance.md. Runtime activation/readiness is separate.' };
}

/** Apply exactly a fresh, revalidated plan. Failure leaves a reservation for explicit recovery. */
export async function applySimpleInit(plan: SimpleInitPlan): Promise<'initialized' | 'already-initialized'> {
  if (plan?.schema !== 'yylo_simple_init_plan.v1' || typeof plan.root !== 'string') throw new Error('Invalid Simple initialization plan.');
  const fresh = await planSimpleInit(plan.root);
  if (JSON.stringify(fresh) !== JSON.stringify(plan)) throw new Error('Stale or modified Simple initialization plan; prepare and inspect a new plan.');
  if (fresh.outcome === 'already-initialized') return 'already-initialized';
  const reservation = path.join(plan.root, RESERVATION);
  await fs.mkdir(reservation, { mode: 0o700 }); // Exclusive: concurrent initializers cannot both apply.
  await fs.writeFile(path.join(reservation, 'plan-sha256'), digest(JSON.stringify(plan)), { flag: 'wx', mode: 0o600 });
  const metadata = path.join(plan.root, '.juno_task');
  await fs.mkdir(metadata, { mode: 0o700 }); // Never replace an existing file/directory, even empty.
  for (const [name, bytes] of Object.entries(fresh.files)) {
    await fs.writeFile(path.join(metadata, name), bytes, { flag: 'wx', mode: 0o600 });
  }
  const { root: _root, ...identity } = await inspectRoot(plan.root);
  if (JSON.stringify(identity) !== JSON.stringify(plan.identity)) throw new Error('Git identity changed during initialization; reservation preserved for explicit inspection.');
  for (const [name, hash] of Object.entries(plan.preserved)) {
    if (await fileDigest(path.join(plan.root, name)) !== hash) throw new Error(`Preserved ${name} changed during initialization; reservation retained.`);
  }
  // Only remove the initializer's own known reservation on successful publication.
  await fs.unlink(path.join(reservation, 'plan-sha256'));
  await fs.rmdir(reservation);
  return 'initialized';
}

export async function writeSimpleInitPlan(file: string, plan: SimpleInitPlan): Promise<void> {
  const destination = path.resolve(file);
  const parent = await fs.realpath(path.dirname(destination));
  const relative = path.relative(plan.root, parent);
  if (!relative || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))) {
    throw new Error('Store the initialization plan outside the project root.');
  }
  await fs.writeFile(path.join(parent, path.basename(destination)), `${JSON.stringify(plan, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
}
