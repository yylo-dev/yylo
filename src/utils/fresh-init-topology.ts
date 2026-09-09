import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'fs-extra';

const CONTROLLER_REF = 'refs/heads/juno/controller-metadata';
const INTEGRATION_AUTHORITY = 'protected-integration.v1';

export interface FreshInitTopologyProbe {
  eligible: boolean;
  repositoryRoot: string | null;
  targetRef: string | null;
}

function git(cwd: string, args: string[], options: { env?: NodeJS.ProcessEnv } = {}): string {
  return execFileSync('git', ['-C', cwd, ...args], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    ...(options.env ? { env: options.env } : {}),
  }).trim();
}

function tryGit(cwd: string, args: string[]): string | null {
  try {
    return git(cwd, args);
  } catch {
    return null;
  }
}

/**
 * Freeze whether init began in an exact, empty, unborn Git worktree. Existing
 * repositories are deliberately excluded so init cannot commit or rearrange
 * owner-controlled bytes while updating an established project.
 */
export function probeFreshInitTopology(targetDirectory: string): FreshInitTopologyProbe {
  const target = path.resolve(targetDirectory);
  const root = tryGit(target, ['rev-parse', '--show-toplevel']);
  const targetRef = tryGit(target, ['symbolic-ref', '--quiet', 'HEAD']);
  const hasHead = tryGit(target, ['rev-parse', '--verify', 'HEAD']) !== null;
  const status = tryGit(target, ['status', '--porcelain=v1', '--untracked-files=all']);
  return {
    eligible: Boolean(root && path.resolve(root) === target && targetRef && !hasHead && status === ''),
    repositoryRoot: root ? path.resolve(root) : null,
    targetRef,
  };
}

export async function installedCliVersion(): Promise<string> {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  for (const candidate of [
    path.resolve(directory, '../../package.json'),
    path.resolve(directory, '../package.json'),
  ]) {
    try {
      const manifest = await fs.readJson(candidate) as { name?: string; version?: string };
      if (manifest.name === '@yylo/cli' && typeof manifest.version === 'string') return manifest.version;
    } catch {
      // Try the source-tree or bundled-runtime layout next.
    }
  }
  throw new Error('fresh init cannot prove the invoking @yylo/cli package version');
}

function integrationOwnerPath(repositoryRoot: string): string {
  const stateRoot = process.env.XDG_STATE_HOME
    ? path.resolve(process.env.XDG_STATE_HOME)
    : path.join(os.homedir(), '.local', 'state');
  const identity = createHash('sha256').update(repositoryRoot).digest('hex').slice(0, 16);
  const name = `${path.basename(repositoryRoot).replace(/[^A-Za-z0-9._-]+/g, '-')}-${identity}`;
  return path.join(stateRoot, 'yylo', 'integration-worktrees', name);
}

/**
 * Materialize the minimum safe controller/product topology for the documented
 * fresh-repository quick start. This is intentionally unavailable to existing,
 * dirty, or already-committed repositories.
 */
export async function establishFreshInitTopology(
  probe: FreshInitTopologyProbe,
  packageVersion: string,
): Promise<{ configured: boolean; integrationOwner: string | null }> {
  if (!probe.eligible || !probe.repositoryRoot || !probe.targetRef) {
    return { configured: false, integrationOwner: null };
  }
  const root = probe.repositoryRoot;
  const owner = integrationOwnerPath(root);
  if (await fs.pathExists(owner)) {
    throw new Error(`fresh init integration-owner path already exists: ${owner}`);
  }
  await fs.ensureDir(path.dirname(owner));

  const commitEnv = {
    ...process.env,
    GIT_AUTHOR_NAME: process.env.GIT_AUTHOR_NAME || 'YYLO Init',
    GIT_AUTHOR_EMAIL: process.env.GIT_AUTHOR_EMAIL || 'init@yylo.invalid',
    GIT_COMMITTER_NAME: process.env.GIT_COMMITTER_NAME || 'YYLO Init',
    GIT_COMMITTER_EMAIL: process.env.GIT_COMMITTER_EMAIL || 'init@yylo.invalid',
  };
  git(root, ['add', '--all']);
  git(root, ['commit', '--no-gpg-sign', '-m', 'chore: initialize yylo workspace'], {
    env: commitEnv,
  });
  const targetSha = git(root, ['rev-parse', `${probe.targetRef}^{commit}`]);
  git(root, ['branch', CONTROLLER_REF.replace(/^refs\/heads\//, ''), targetSha]);
  git(root, ['switch', CONTROLLER_REF.replace(/^refs\/heads\//, '')]);
  git(root, ['worktree', 'add', '--detach', owner, targetSha]);

  git(root, ['config', '--local', 'extensions.worktreeConfig', 'true']);
  git(root, ['config', '--local', 'juno.controller.path', root]);
  git(root, ['config', '--local', 'juno.controller.branch', CONTROLLER_REF]);
  git(root, ['config', '--local', 'juno.integration.ownerPath', owner]);
  git(root, ['config', '--worktree', 'juno.workspace.role', 'controller']);
  git(root, ['config', '--worktree', 'juno.controller.runtimeVersion', packageVersion]);
  git(root, ['config', '--worktree', 'juno.controller.generation', packageVersion]);
  git(owner, ['config', '--worktree', 'juno.workspace.role', 'integration-owner']);
  git(owner, ['config', '--worktree', 'juno.workspace.roleAuthority', INTEGRATION_AUTHORITY]);
  git(owner, ['config', '--worktree', 'juno.workspace.roleBase', targetSha]);

  return { configured: true, integrationOwner: owner };
}
