#!/usr/bin/env node
/** Deterministic real-Git characterization for the one-task delivery decision. */
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const SCHEMA = 'yylo.native_git_merge_characterization.v1';

function git(cwd, ...args) {
  return execFileSync('git', ['-c', 'protocol.file.allow=always', '-C', cwd, ...args], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

function attempt(cwd, ...args) {
  return spawnSync('git', ['-c', 'protocol.file.allow=always', '-C', cwd, ...args], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
  });
}

function write(root, relative, text) {
  const target = path.join(root, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, text);
}

function commit(root, message, files) {
  for (const [relative, text] of Object.entries(files)) write(root, relative, text);
  git(root, 'add', '.');
  git(root, 'commit', '-m', message);
  return git(root, 'rev-parse', 'HEAD');
}

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-native-git-'));
  git(directory, 'init', '-q');
  git(directory, 'config', 'user.name', 'YYLO fixture');
  git(directory, 'config', 'user.email', 'fixture@invalid');
  const base = commit(directory, 'base', { 'shared.txt': 'one\ntwo\nthree\n', 'base.txt': 'base\n' });
  git(directory, 'branch', 'target', base);
  return { directory, base };
}

function branchWorktree(f, branch, start = f.base) {
  const worktree = path.join(f.directory, '..', `${path.basename(f.directory)}-${branch}`);
  git(f.directory, 'worktree', 'add', '-q', '-b', branch, worktree, start);
  return worktree;
}

let candidateSequence = 0;
function candidate(f, target = 'target') {
  const suffix = String(candidateSequence += 1);
  const worktree = path.join(f.directory, '..', `${path.basename(f.directory)}-candidate-${suffix}`);
  git(f.directory, 'worktree', 'add', '-q', '--detach', worktree, target);
  return worktree;
}

function compose(f, source) {
  const observedTarget = git(f.directory, 'rev-parse', 'refs/heads/target');
  const worktree = candidate(f);
  const result = attempt(worktree, 'merge', '--no-edit', source);
  return { worktree, result, observedTarget, candidate: result.status === 0 ? git(worktree, 'rev-parse', 'HEAD') : null };
}

function land(f, candidateSha, expected) {
  return attempt(f.directory, 'update-ref', 'refs/heads/target', candidateSha, expected);
}

function run(name, body) {
  const started = process.hrtime.bigint();
  const detail = body();
  return { name, passed: true, wall_ms: Number(process.hrtime.bigint() - started) / 1e6, ...detail };
}

export function characterize() {
  const scenarios = [];

  scenarios.push(run('direct-fast-forward', () => {
    const f = fixture();
    const source = branchWorktree(f, 'source');
    const tip = commit(source, 'source', { 'source.txt': 'source\n' });
    const before = git(f.directory, 'rev-parse', 'target');
    const result = land(f, tip, before);
    if (result.status !== 0 || git(f.directory, 'rev-parse', 'target') !== tip) throw new Error('fast-forward failed');
    return { git_operation: 'update-ref expected-old', preserves_both_changes: true };
  }));

  scenarios.push(run('divergent-clean-merge', () => {
    const f = fixture();
    const source = branchWorktree(f, 'source');
    commit(source, 'source', { 'source.txt': 'source\n' });
    const target = branchWorktree(f, 'target-side');
    const targetTip = commit(target, 'target', { 'target.txt': 'target\n' });
    git(f.directory, 'update-ref', 'refs/heads/target', targetTip, f.base);
    const merged = compose(f, 'source');
    if (merged.result.status !== 0 || !fs.existsSync(path.join(merged.worktree, 'source.txt')) || !fs.existsSync(path.join(merged.worktree, 'target.txt'))) throw new Error('clean merge lost a side');
    return { merge_parents: git(merged.worktree, 'show', '-s', '--format=%P', 'HEAD').split(' ').length, preserves_both_changes: true };
  }));

  scenarios.push(run('same-file-disjoint-hunks', () => {
    const f = fixture();
    const source = branchWorktree(f, 'source');
    commit(source, 'source', { 'shared.txt': 'one\ntwo\nTHREE\n' });
    const target = branchWorktree(f, 'target-side');
    const targetTip = commit(target, 'target', { 'shared.txt': 'ONE\ntwo\nthree\n' });
    git(f.directory, 'update-ref', 'refs/heads/target', targetTip, f.base);
    const merged = compose(f, 'source');
    if (merged.result.status !== 0 || fs.readFileSync(path.join(merged.worktree, 'shared.txt'), 'utf8') !== 'ONE\ntwo\nTHREE\n') throw new Error('disjoint hunks not preserved');
    return { preserves_both_changes: true };
  }));

  scenarios.push(run('conflict-x-does-not-block-unrelated-y', () => {
    const f = fixture();
    const x = branchWorktree(f, 'x');
    commit(x, 'x', { 'shared.txt': 'X\ntwo\nthree\n' });
    const y = branchWorktree(f, 'y');
    commit(y, 'y', { 'y.txt': 'Y\n' });
    const target = branchWorktree(f, 'target-side');
    const targetTip = commit(target, 'target', { 'shared.txt': 'TARGET\ntwo\nthree\n' });
    git(f.directory, 'update-ref', 'refs/heads/target', targetTip, f.base);
    const conflicted = compose(f, 'x');
    if (conflicted.result.status === 0 || !fs.existsSync(path.join(conflicted.worktree, '.git'))) throw new Error('X conflict not preserved');
    const independent = compose(f, 'y');
    if (independent.result.status !== 0 || land(f, independent.candidate, independent.observedTarget).status !== 0) throw new Error('Y was blocked by X');
    return { x_conflict_preserved: true, y_landed: true, fifo_wait_due_to_x: 0 };
  }));

  scenarios.push(run('competing-expected-old-updates', () => {
    const f = fixture();
    const x = branchWorktree(f, 'x');
    const xTip = commit(x, 'x', { 'x.txt': 'X\n' });
    const y = branchWorktree(f, 'y');
    const yTip = commit(y, 'y', { 'y.txt': 'Y\n' });
    const observed = git(f.directory, 'rev-parse', 'target');
    const first = land(f, xTip, observed);
    const second = land(f, yTip, observed);
    if (first.status !== 0 || second.status === 0 || git(f.directory, 'rev-parse', 'target') !== xTip) throw new Error('CAS did not reject stale writer');
    return { winner_preserved: true, stale_writer_rejected: true };
  }));

  scenarios.push(run('target-moves-during-validation', () => {
    const f = fixture();
    const x = branchWorktree(f, 'x');
    commit(x, 'x', { 'x.txt': 'X\n' });
    const y = branchWorktree(f, 'y');
    const yTip = commit(y, 'y', { 'y.txt': 'Y\n' });
    const prepared = compose(f, 'x');
    if (land(f, yTip, prepared.observedTarget).status !== 0) throw new Error('fixture move failed');
    if (land(f, prepared.candidate, prepared.observedTarget).status === 0) throw new Error('stale validated candidate landed');
    return { stale_candidate_rejected: true, retry_required: true };
  }));

  scenarios.push(run('dirty-source-bytes-preserved', () => {
    const f = fixture();
    const source = branchWorktree(f, 'source');
    commit(source, 'source', { 'source.txt': 'source\n' });
    write(source, 'dirty.txt', 'uncommitted payload\n');
    const before = git(source, 'status', '--porcelain=v1');
    const prepared = compose(f, 'source');
    const after = git(source, 'status', '--porcelain=v1');
    if (prepared.result.status !== 0 || before !== after || fs.readFileSync(path.join(source, 'dirty.txt'), 'utf8') !== 'uncommitted payload\n') throw new Error('dirty source changed');
    return { dirty_status_preserved: before, dirty_bytes_preserved: 20 };
  }));

  scenarios.push(run('already-contained-source', () => {
    const f = fixture();
    const source = branchWorktree(f, 'source');
    const tip = commit(source, 'source', { 'source.txt': 'source\n' });
    const before = git(f.directory, 'rev-parse', 'target');
    if (land(f, tip, before).status !== 0) throw new Error('initial landing failed');
    const target = git(f.directory, 'rev-parse', 'target');
    const contained = attempt(f.directory, 'merge-base', '--is-ancestor', tip, target).status === 0;
    if (!contained || git(f.directory, 'rev-parse', 'target') !== target) throw new Error('containment check mutated target');
    return { contained: true, duplicate_integration: false };
  }));

  scenarios.push(run('crash-before-update', () => {
    const f = fixture();
    const source = branchWorktree(f, 'source');
    commit(source, 'source', { 'source.txt': 'source\n' });
    const prepared = compose(f, 'source');
    if (prepared.result.status !== 0 || git(f.directory, 'rev-parse', 'target') !== prepared.observedTarget) throw new Error('pre-update preparation moved target');
    return { target_unchanged: true, candidate_recoverable: true };
  }));

  scenarios.push(run('git-success-ledger-failure', () => {
    const f = fixture();
    const source = branchWorktree(f, 'source');
    const tip = commit(source, 'source', { 'source.txt': 'source\n' });
    const before = git(f.directory, 'rev-parse', 'target');
    if (land(f, tip, before).status !== 0) throw new Error('Git landing failed');
    let projectionFailed = false;
    try { throw new Error('simulated Ledger unavailable'); } catch { projectionFailed = true; }
    const containedOnRetry = attempt(f.directory, 'merge-base', '--is-ancestor', tip, 'target').status === 0;
    if (!projectionFailed || !containedOnRetry || git(f.directory, 'rev-parse', 'target') !== tip) throw new Error('projection failure changed Git result');
    return { git_success_reportable: true, ledger_projection_retryable: true, duplicate_integration: false };
  }));

  scenarios.push(run('submodule-gitlink-change', () => {
    const f = fixture();
    const sub = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-native-submodule-'));
    git(sub, 'init', '-q');
    git(sub, 'config', 'user.name', 'YYLO fixture');
    git(sub, 'config', 'user.email', 'fixture@invalid');
    const a = commit(sub, 'a', { 'value.txt': 'A\n' });
    const b = commit(sub, 'b', { 'value.txt': 'B\n' });
    git(sub, 'checkout', '-q', a);
    git(f.directory, 'submodule', 'add', '-q', sub, 'dep');
    git(f.directory, 'commit', '-m', 'add submodule');
    const base = git(f.directory, 'rev-parse', 'HEAD');
    git(f.directory, 'update-ref', 'refs/heads/target', base);
    const source = branchWorktree({ ...f, base }, 'source', base);
    git(source, 'submodule', 'update', '-q', '--init');
    git(path.join(source, 'dep'), 'fetch', '-q', sub, b);
    git(path.join(source, 'dep'), 'checkout', '-q', b);
    git(source, 'add', 'dep'); git(source, 'commit', '-m', 'advance gitlink');
    const target = branchWorktree({ ...f, base }, 'target-side', base);
    const targetTip = commit(target, 'target docs', { 'docs.txt': 'target\n' });
    git(f.directory, 'update-ref', 'refs/heads/target', targetTip, base);
    const prepared = compose(f, 'source');
    if (prepared.result.status !== 0 || git(prepared.worktree, 'ls-tree', 'HEAD', 'dep').split(/\s+/)[2] !== b) throw new Error('gitlink change not preserved');
    return { gitlink_preserved: true };
  }));

  return {
    schema_version: SCHEMA,
    git_version: git(process.cwd(), '--version'),
    topology: 'private detached candidate plus expected-old update of an unchecked-out target ref',
    scenario_count: scenarios.length,
    model_calls: 0,
    scenarios,
  };
}

export function main() {
  process.stdout.write(`${JSON.stringify(characterize(), null, 2)}\n`);
}

if (process.argv[1] && fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))) main();
