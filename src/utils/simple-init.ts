import path from 'node:path';
import fs from 'node:fs/promises';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { SIMPLE_FILES, SIMPLE_GUIDANCE } from '../templates/simple-workspace.js';
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

export interface SimpleInitSettings {
  task?: string;
  subagent?: string;
}

export interface SimpleInitPlan {
  schema: 'yylo_simple_init_plan.v1';
  outcome: 'ready' | 'already-initialized';
  root: string;
  identity: { commonDir: string; head: string | null; ref: string | null; gitConfig: string | null };
  preserved: Record<string, string | null>;
  files: Record<string, string>;
  guidance: string;
  settings?: SimpleInitSettings;
}

function generatedFiles(settings?: SimpleInitSettings): Record<string, string> {
  const files = { ...SIMPLE_FILES };
  if (settings) {
    if (Object.keys(settings).some((key) => !['task', 'subagent'].includes(key)) ||
        (settings.task !== undefined && typeof settings.task !== 'string') ||
        (settings.subagent !== undefined && !['claude', 'codex', 'gemini', 'cursor', 'pi'].includes(settings.subagent))) {
      throw new Error('Invalid Simple initialization settings.');
    }
    if (settings.subagent) {
      files['config.json'] = `${JSON.stringify({ ...JSON.parse(files['config.json']!), defaultSubagent: settings.subagent }, null, 2)}\n`;
    }
    if (settings.task) files['simple-agent-guidance.md'] += `\n## Initial project goal\n\n${settings.task}\n`;
  }
  return { ...files, [RECEIPT]: `${JSON.stringify({ schema: 'yylo_simple_initialization.v1',
    files: Object.fromEntries(Object.entries(files).map(([name, content]) => [name, digest(content)])),
  }, null, 2)}\n` };
}

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
export async function planSimpleInit(directory: string, settings?: SimpleInitSettings): Promise<SimpleInitPlan> {
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
  const files = generatedFiles(settings);
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
    ...(settings ? { settings } : {}),
    guidance: 'Preserve root AGENTS.md/CLAUDE.md; Simple agents must also read .juno_task/simple-agent-guidance.md. Runtime activation/readiness is separate.' };
}

/** Apply exactly a fresh, revalidated plan. Failure leaves a reservation for explicit recovery. */
export async function applySimpleInit(plan: SimpleInitPlan): Promise<'initialized' | 'already-initialized'> {
  if (plan?.schema !== 'yylo_simple_init_plan.v1' || typeof plan.root !== 'string') throw new Error('Invalid Simple initialization plan.');
  const fresh = await planSimpleInit(plan.root, plan.settings);
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

export async function writeSimpleInitPlan(file: string, plan: SimpleInitPlan | SimpleConversionPlan): Promise<void> {
  const destination = path.resolve(file);
  const parent = await fs.realpath(path.dirname(destination));
  const protectedRoots = plan.schema === 'yylo_simple_conversion_plan.v1'
    ? [plan.root, plan.source.root, plan.source.commonDir, ...plan.source.worktrees.split('\0').filter((entry) => entry.startsWith('worktree ')).map((entry) => entry.slice(9))] : [plan.root];
  if (protectedRoots.some((root) => isWithin(root, parent))) {
    throw new Error('Store the initialization plan outside the project root and source Git storage.');
  }
  const bytes = `${JSON.stringify(plan, null, 2)}\n`;
  if (Buffer.byteLength(bytes) > 4194304) throw new Error('Simple plan exceeds the supported 4 MiB limit.');
  await fs.writeFile(path.join(parent, path.basename(destination)), bytes, { flag: 'wx', mode: 0o600 });
}

// One narrow snapshot transition. Never mutate a source workspace or registration.
const DURABLE_ROOTS = ['tasks', 'ledger', 'archive', 'archive-receipts', 'documents',
  'document-ledger', 'artifacts', 'artifact-ledger', 'objects', 'wiki', 'specs', 'workflows', 'tasks.md'];
const INACTIVE_ROOTS = ['.juno_task', '.agents', '.claude', '.pi', '.codex', '.cursor', '.gemini', 'AGENTS.md', 'CLAUDE.md'];
const RETAINED = '.juno_task/advanced-backup';
const CONVERSION_RECEIPT = '.juno_task/simple-conversion.json';
const isWithin = (root: string, file: string): boolean => {
  const relative = path.relative(root, file);
  return !relative || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
};
const belongsTo = (file: string, roots: readonly string[]) => roots.some((root) => file === root || file.startsWith(`${root}/`));

interface GitFile { path: string; mode: string; oid: string }
export interface SimpleConversionPlan {
  schema: 'yylo_simple_conversion_plan.v1';
  root: string;
  source: { root: string; commonDir: string; head: string; targetRef: string; targetSha: string;
    refs: string; worktrees: string; config: string; worktreeConfig: string | null };
  copy: GitFile[];
  retain: string[];
  productFiles: number;
  generatedSha256: string;
  instructions: string;
}

function conversionEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  // The explicitly selected new checkout must not inherit Git routing, filters,
  // global hooks, or managed controller assertions. Source admission happens first.
  for (const key of Object.keys(env)) if (/^(GIT_|JUNO_|YYLO_)/.test(key)) delete env[key];
  return { ...env, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0', GIT_OPTIONAL_LOCKS: '0' };
}
function conversionGit(root: string, args: string[]): Buffer {
  return execFileSync('git', ['-c', 'core.hooksPath=/dev/null', '-c', 'core.quotePath=false', '-C', root, ...args], {
    env: conversionEnv(), timeout: 120_000, maxBuffer: 16 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'],
  });
}
const conversionText = (root: string, args: string[]) => conversionGit(root, args).toString('utf8').trim();
function treeFiles(root: string, revision: string): GitFile[] {
  const bytes = conversionGit(root, ['ls-tree', '-rz', '--full-tree', revision]);
  const text = bytes.toString('utf8');
  if (!Buffer.from(text).equals(bytes)) throw new Error('Conversion requires UTF-8 Git paths; no lossy filename conversion.');
  return text.split('\0').filter(Boolean).map((line) => {
    const match = /^(100644|100755) blob ([a-f0-9]+)\t(.+)$/s.exec(line);
    if (!match) throw new Error('Conversion currently refuses symlinks and submodules; preserve the source and use a separately reviewed transition.');
    const name = match[3]!;
    if (name.split('/').some((part) => ['..', '.', '.git'].includes(part)) || path.isAbsolute(name) || name.includes('\\')) throw new Error('Unsupported Git tree path.');
    return { path: name, mode: match[1]!, oid: match[2]! };
  });
}
async function readSourceJson(root: string, file: string): Promise<any> {
  if (!await fileDigest(path.join(root, file))) throw new Error(`Missing required Advanced configuration: ${file}`);
  const bytes = await fs.readFile(path.join(root, file));
  if (!bytes.equals(conversionGit(root, ['show', `HEAD:${file}`]))) throw new Error(`Advanced configuration must match committed HEAD: ${file}`);
  const parsed = JSON.parse(bytes.toString('utf8'));
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error(`Expected a JSON object: ${file}`);
  return parsed;
}
async function inspectConversionSource(directory: string): Promise<SimpleConversionPlan['source']> {
  if (Object.keys(process.env).some((key) => /^(GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_INDEX_FILE|GIT_CONFIG.*)$/.test(key))) {
    throw new Error('Conversion refuses inherited Git routing/config overrides. Use the ordinary source checkout environment.');
  }
  const root = await fs.realpath(path.resolve(directory));
  const resolved = resolveController(root, 'diagnostic', { trustedResolver: true });
  if (!resolved.valid || resolved.role !== 'controller' || await fs.realpath(resolved.path) !== root) {
    throw new Error('Conversion requires the exact registered Advanced controller, not Simple, a task worktree or an unregistered project.');
  }
  if (conversionText(root, ['rev-parse', '--show-toplevel']) !== root) throw new Error('Use the exact controller root.');
  const config = await readSourceJson(root, '.juno_task/config.json');
  if (config.controllerWorkspace?.mode !== 'metadata-only' || config.controllerWorkspace?.policy !== '.juno_task/config/metadata-controller.json' || Object.keys(config.controllerWorkspace).length !== 2) {
    throw new Error('Only canonical metadata-only Advanced controllers are supported.');
  }
  const policy = await readSourceJson(root, '.juno_task/config/metadata-controller.json');
  const workspace = await readSourceJson(root, '.juno_task/config/task-workspace.json');
  const targetRef = workspace.target_ref;
  if (workspace.schema_version !== 'juno_task_workspace_config.v1' || policy.schema_version !== 'juno_metadata_controller_policy.v1' || workspace.repository !== '.' || typeof targetRef !== 'string' || !targetRef.startsWith('refs/heads/') || policy.product_ref !== targetRef || policy.controller_branch === targetRef) {
    throw new Error('Conversion requires a same-repository product branch agreed by the controller and task-workspace policies.');
  }
  conversionGit(root, ['check-ref-format', '--branch', targetRef.slice(11)]);
  const branch = conversionText(root, ['symbolic-ref', 'HEAD']);
  if (branch !== policy.controller_branch || conversionText(root, ['config', '--local', '--get', 'juno.controller.branch']) !== branch ||
      await fs.realpath(conversionText(root, ['config', '--local', '--get', 'juno.controller.path'])) !== root) {
    throw new Error('Controller registration/branch does not match its policy.');
  }
  const state = await readSourceJson(root, '.juno_task/state/tasks.json');
  if (state.schema_version !== 'juno_task_workspace_state.v2' || !state.tasks || Array.isArray(state.tasks) || typeof state.tasks !== 'object') throw new Error('Unsupported lifecycle state; no conversion performed.');
  for (const [id, record] of Object.entries(state.tasks)) {
    if (!record || !['MERGED', 'WITHDRAWN'].includes((record as { state?: string }).state || '')) {
      throw new Error(`Unfinished or unknown lifecycle task ${id}; settle managed work before conversion.`);
    }
  }
  const worktrees = conversionText(root, ['worktree', 'list', '--porcelain', '-z']);
  for (const entry of worktrees.split('\0').filter((line) => line.startsWith('worktree '))) {
    const worktree = entry.slice(9);
    if (conversionGit(worktree, ['ls-files', '-v', '-z']).toString('utf8').split('\0').some((file) => /^[a-zS] /.test(file))) {
      throw new Error(`Sparse/assume-unchanged source index requires manual review: ${worktree}`);
    }
    if (conversionText(worktree, ['status', '--porcelain=v1', '--untracked-files=all'])) throw new Error(`Dirty source worktree: ${worktree}. Commit or preserve work explicitly before conversion.`);
  }
  const durablePaths = DURABLE_ROOTS.map((name) => `.juno_task/${name}`);
  if (conversionText(root, ['ls-files', '--others', '--ignored', '--exclude-standard', '--', ...durablePaths])) {
    throw new Error('Ignored durable controller data would be omitted; review and commit it explicitly before conversion.');
  }
  const commonDir = await fs.realpath(conversionText(root, ['rev-parse', '--path-format=absolute', '--git-common-dir']));
  const gitDir = await fs.realpath(conversionText(root, ['rev-parse', '--path-format=absolute', '--git-dir']));
  return { root, commonDir, head: conversionText(root, ['rev-parse', 'HEAD']), targetRef,
    targetSha: conversionText(root, ['rev-parse', `${targetRef}^{commit}`]),
    refs: conversionText(root, ['show-ref']), worktrees,
    config: (await fileDigest(path.join(commonDir, 'config')))!, worktreeConfig: await fileDigest(path.join(gitDir, 'config.worktree')) };
}

/** Read-only plan; product content always comes from the policy-selected target. */
export async function planSimpleConversion(controller: string, destination: string): Promise<SimpleConversionPlan> {
  const source = await inspectConversionSource(controller);
  const requested = path.resolve(destination);
  const root = path.join(await fs.realpath(path.dirname(requested)), path.basename(requested));
  await assertFreshDestination(root, source);
  const product = treeFiles(source.root, source.targetSha);
  const controllerFiles = treeFiles(source.root, source.head);
  for (const file of product.filter((item) => !belongsTo(item.path, INACTIVE_ROOTS))) {
    if (/\/(?:\.agents|\.claude|\.pi|\.codex|\.cursor|\.gemini)\//.test(file.path) ||
        (/\/(?:AGENTS|CLAUDE)\.md$/.test(file.path) && /yy\s+task\s+(?:start|finish)|metadata[- ]only|integration[- ]owner|mandatory.*worktree/i.test(conversionGit(source.root, ['cat-file', 'blob', file.oid]).toString('utf8')))) {
      throw new Error(`Nested agent configuration or managed instructions require manual review before conversion: ${file.path}`);
    }
  }
  if (product.some((file) => file.path === '.yylo-simple-init' || file.path.startsWith('.yylo-simple-init/'))) throw new Error('Product has an initialization reservation collision.');
  // Tracked secrets would still exist in Git history. Refuse known active secret
  // paths rather than pretending conversion can sanitize repository history.
  if (product.some((file) => /(^|\/)\.env(?:$|\.(?!example$|sample$))|^\.juno_task\/(secrets|runtime|sessions|cache|logs)\//.test(file.path))) {
    throw new Error('Product tracks secrets or live runtime data. Review it explicitly; conversion is not a credential/history sanitizer.');
  }
  const copy = controllerFiles.filter((file) => belongsTo(file.path, DURABLE_ROOTS.map((name) => `.juno_task/${name}`)));
  const productDurable = product.filter((file) => belongsTo(file.path, DURABLE_ROOTS.map((name) => `.juno_task/${name}`)));
  if (productDurable.length) throw new Error('Product also contains durable Ledger/knowledge data; automatic board merging is unsupported.');
  const retain = INACTIVE_ROOTS.filter((name) => product.some((file) => belongsTo(file.path, [name])));
  if (product.some((file) => file.path === '.gitignore')) retain.push('.gitignore');
  if (copy.some((file) => file.path === RETAINED || file.path.startsWith(`${RETAINED}/`))) throw new Error('Retained instruction path collision.');
  // Preflight source again, including quiet working trees, after inventory.
  if (JSON.stringify(await inspectConversionSource(controller)) !== JSON.stringify(source)) throw new Error('Source changed during conversion planning.');
  return { schema: 'yylo_simple_conversion_plan.v1', root, source, copy, retain, productFiles: product.length,
    generatedSha256: digest(JSON.stringify(generatedFiles())),
    instructions: 'Stop agents/writers before apply. Copy only committed durable data; keep all source workspaces unchanged. Retain old instructions/config under .juno_task/advanced-backup, inactive. Review custom guidance, models/hooks and dependencies manually. No automatic remote, staging, commit, cleanup or live cutover. Simple-to-Advanced is unsupported.' };
}
async function assertFreshDestination(root: string, source: SimpleConversionPlan['source']): Promise<void> {
  const sourceRoots = [source.root, source.commonDir, ...source.worktrees.split('\0').filter((entry) => entry.startsWith('worktree ')).map((entry) => entry.slice(9))];
  if (sourceRoots.some((location) => isWithin(location, root) || isWithin(root, location))) throw new Error('Destination must be separate from all source worktrees and Git storage.');
  try { await fs.lstat(root); throw new Error('Conversion destination must not exist; preserve any prior attempt and choose a fresh folder.'); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  let ancestor = path.dirname(root);
  while (true) {
    try { await fs.lstat(path.join(ancestor, '.juno_task')); throw new Error('Destination is nested inside an existing workspace.'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    if (ancestor === path.dirname(ancestor)) break;
    ancestor = path.dirname(ancestor);
  }
}

/** Create a new standalone checkout; failure preserves it, never touches source. */
export async function applySimpleConversion(plan: SimpleConversionPlan): Promise<'converted'> {
  if (plan?.schema !== 'yylo_simple_conversion_plan.v1' || typeof plan.root !== 'string' || typeof plan.source?.root !== 'string') throw new Error('Invalid Simple conversion plan.');
  const fresh = await planSimpleConversion(plan.source.root, plan.root);
  if (JSON.stringify(fresh) !== JSON.stringify(plan)) throw new Error('Stale or modified conversion plan; prepare a new preview.');
  await fs.mkdir(plan.root, { mode: 0o700 }); // Exclusive; failed attempts are never removed.
  conversionGit(plan.source.root, ['clone', '--no-local', '--no-checkout', '--single-branch', '--branch', plan.source.targetRef.slice(11), '--', plan.source.root, plan.root]);
  const reservation = path.join(plan.root, RESERVATION);
  await fs.mkdir(reservation, { mode: 0o700 });
  await fs.writeFile(path.join(reservation, 'plan-sha256'), digest(JSON.stringify(plan)), { flag: 'wx', mode: 0o600 });
  conversionGit(plan.root, ['checkout', '--quiet', plan.source.targetRef.slice(11)]);
  if (conversionText(plan.root, ['rev-parse', 'HEAD']) !== plan.source.targetSha) throw new Error('Cloned product identity mismatch; destination preserved.');
  conversionGit(plan.root, ['remote', 'remove', 'origin']); // Never push back into the managed source by accident.
  // The backup starts outside metadata so moving .juno_task cannot nest into itself.
  const backup = path.join(reservation, 'advanced-backup');
  await fs.mkdir(backup, { mode: 0o700 });
  for (const name of plan.retain) {
    const file = path.join(plan.root, name);
    if (name === '.gitignore') await fs.copyFile(file, path.join(backup, name));
    else await fs.rename(file, path.join(backup, name));
  }
  await fs.mkdir(path.join(plan.root, '.juno_task'), { mode: 0o700 });
  await fs.rename(backup, path.join(plan.root, RETAINED));
  for (const file of plan.copy) {
    const destination = path.join(plan.root, file.path);
    await fs.mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
    await fs.writeFile(destination, conversionGit(plan.source.root, ['cat-file', 'blob', file.oid]), { flag: 'wx', mode: file.mode === '100755' ? 0o700 : 0o600 });
  }
  const activeGuidance = `${SIMPLE_GUIDANCE}\n## Retained Advanced instructions\n\nOld root instructions, agent configuration and product metadata are inactive under\n.juno_task/advanced-backup. Review project-specific tests and coding conventions\nmanually; do not restore managed lifecycle hooks or run archived scripts.\nController wiki/specs are preserved knowledge, not current workspace authority.\n`;
  for (const name of ['AGENTS.md', 'CLAUDE.md']) await fs.writeFile(path.join(plan.root, name), activeGuidance, { flag: 'wx', mode: 0o600 });
  await fs.appendFile(path.join(plan.root, '.gitignore'), '\n# Simple conversion: keep durable metadata visible.\n!/.juno_task/\n!/.juno_task/**\n', { mode: 0o600 });
  for (const [name, bytes] of Object.entries(generatedFiles())) {
    await fs.writeFile(path.join(plan.root, '.juno_task', name), bytes, { flag: 'wx', mode: 0o600 });
  }
  for (const file of [...plan.copy.map((item) => item.path), '.juno_task/config.json', CONVERSION_RECEIPT]) {
    const ignored = spawnSync('git', ['-C', plan.root, 'check-ignore', '--no-index', '--', file], { env: conversionEnv(), encoding: 'utf8', timeout: 10_000 });
    if (ignored.status !== 1) throw new Error(`Cannot prove durable destination path is Git-visible: ${file}. Destination reservation retained.`);
  }
  if (JSON.stringify(await inspectConversionSource(plan.source.root)) !== JSON.stringify(plan.source)) throw new Error('Source changed during conversion; destination reservation retained.');
  await fs.writeFile(path.join(plan.root, CONVERSION_RECEIPT), `${JSON.stringify({ schema: 'yylo_simple_conversion.v1', plan }, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
  // Only successful completion removes the known reservation. No worktree cleanup.
  await fs.unlink(path.join(reservation, 'plan-sha256'));
  await fs.rmdir(reservation);
  try {
    const resolution = resolveController(plan.root, 'diagnostic', { trustedResolver: true, env: conversionEnv() });
    if (!resolution.valid || resolution.role !== 'simple') throw new Error('Converted workspace failed Simple validation.');
  } catch (error) {
    await fs.mkdir(reservation, { mode: 0o700 });
    throw error;
  }
  return 'converted';
}
