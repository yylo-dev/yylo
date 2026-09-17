import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import fs from 'fs-extra';
import { afterEach, describe, expect, it } from 'vitest';
import { selectTaskWorkspaceRuntime, withTaskPythonBytecodeBoundary } from '../commands/task.js';

const roots: string[] = [];

async function fixture(): Promise<{ controller: string; canonical: string; packaged: string }> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-task-hydrate-'));
  roots.push(root);
  const controller = path.join(root, 'controller');
  const scripts = path.join(controller, '.juno_task', 'scripts');
  const packagedRoot = path.join(root, 'package', 'templates', 'scripts');
  const canonical = path.join(scripts, 'task_workspace.py');
  const packaged = path.join(packagedRoot, 'task_workspace.py');
  await fs.ensureDir(scripts);
  await fs.ensureDir(packagedRoot);
  await fs.writeFile(canonical, [
    'import sys',
    'print("unsupported task audit operation: hydrate", file=sys.stderr)',
    'raise SystemExit(2)',
  ].join('\n'));
  await fs.writeFile(packaged, [
    'TASK_HYDRATE_RECOVERY_SCHEMA = "juno_task_hydrate_recovery.v1"',
    // Stable capability marker matched by selectTaskWorkspaceRuntime: the
    // audited operation list may evolve without invalidating selection.
    'TASK_RUNTIME_CAPABILITY_HYDRATE_V1 = True',
    'def hydrate(controller: object, task_id: str): pass',
    'AUDITED = ("start", "status", "hydrate", "preflight", "finish")',
    'ROUTED = ("start", "status", "hydrate", "preflight", "finish",)',
  ].join('\n'));
  await fs.writeFile(path.join(packagedRoot, 'workflow_runner.sh'), '# packaged runner\n');
  return { controller, canonical, packaged };
}

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => fs.remove(root)));
});

describe('task hydrate recovery runtime routing', () => {
  it('neither consumes sibling bytecode nor writes into packaged modules', async () => {
    const { packaged } = await fixture();
    const directory = path.dirname(packaged);
    const helper = path.join(directory, 'helper.py');
    const execute = promisify(execFile);
    await fs.writeFile(helper, 'value = "bad!"\n');
    const stamp = await fs.stat(helper);
    await execute('python3', ['-c', 'import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)', helper]);
    await fs.writeFile(helper, 'value = "good"\n');
    await fs.utimes(helper, stamp.atime, stamp.mtime);
    await fs.writeFile(path.join(directory, 'fresh.py'), 'value = 42\n');
    await fs.writeFile(packaged, 'import helper, fresh\nassert helper.value == "good"\nassert fresh.value == 42\n');
    const before = await fs.readdir(path.join(directory, '__pycache__'));
    let cache = '';
    await withTaskPythonBytecodeBoundary(process.env, async (flags, env) => {
      cache = env.PYTHONPYCACHEPREFIX!;
      expect(env.PYTHONDONTWRITEBYTECODE).toBe('1');
      await execute('python3', [...flags, packaged], { env });
    });
    expect(await fs.readdir(path.join(directory, '__pycache__'))).toEqual(before);
    expect(await fs.pathExists(cache)).toBe(false);
  });

  it('uses the protocol-checked package runtime instead of a stale selected runtime', async () => {
    const { controller, packaged } = await fixture();
    await expect(selectTaskWorkspaceRuntime(controller, 'hydrate', [packaged]))
      .resolves.toBe(packaged);
  });

  it('keeps ordinary task operations bound to the selected controller runtime', async () => {
    const { controller, canonical, packaged } = await fixture();
    await expect(selectTaskWorkspaceRuntime(controller, 'start', [packaged]))
      .resolves.toBe(canonical);
    await expect(selectTaskWorkspaceRuntime(controller, 'status', [packaged]))
      .resolves.toBe(canonical);
  });

  it('fails closed when the packaged hydrate protocol or runner is incomplete', async () => {
    const { controller, packaged } = await fixture();
    await fs.writeFile(packaged, '# incompatible package runtime\n');
    await expect(selectTaskWorkspaceRuntime(controller, 'hydrate', [packaged]))
      .rejects.toThrow('incompatible; refusing stale controller fallback');

    const complete = await fixture();
    await fs.remove(path.join(path.dirname(complete.packaged), 'workflow_runner.sh'));
    await expect(selectTaskWorkspaceRuntime(complete.controller, 'hydrate', [complete.packaged]))
      .rejects.toThrow('incomplete; refusing stale controller fallback');
  });
});
