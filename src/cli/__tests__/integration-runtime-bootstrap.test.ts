import { execFileSync, spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import fs from 'fs-extra';
import { afterEach, describe, expect, it } from 'vitest';
import { selectIntegrationRuntime } from '../commands/integration.js';

const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => fs.remove(root)));
});

async function fixture(): Promise<{ controller: string; canonical: string; packaged: string; marker: string }> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-runtime-bootstrap-'));
  roots.push(root);
  const controller = path.join(root, 'controller');
  const scripts = path.join(controller, '.juno_task', 'scripts');
  const packagedRoot = path.join(root, 'package', 'templates', 'scripts');
  const canonical = path.join(scripts, 'integration_workspace.py');
  const packaged = path.join(packagedRoot, 'integration_workspace.py');
  const marker = path.join(root, 'packaged-ran.json');
  await fs.ensureDir(scripts);
  await fs.ensureDir(packagedRoot);
  await fs.writeFile(canonical, [
    'import argparse',
    'p=argparse.ArgumentParser()',
    'p.add_argument("--controller")',
    's=p.add_subparsers(dest="operation", required=True)',
    'r=s.add_parser("runtime-refresh")',
    'r.add_argument("--previous-sha", required=True)',
    'r.add_argument("--target-sha")',
    'p.parse_args()',
  ].join('\n'));
  await fs.writeFile(packaged, [
    'import argparse,json,pathlib',
    'MANAGED_REPAIR_SCHEMA = "juno_managed_runtime_repair.v1"',
    'SOURCE_ADOPTION_SCHEMA = "juno_source_runtime_adoption.v1"',
    'def managed_runtime_repair_plan(): pass',
    'def source_runtime_adopt(): pass',
    'p=argparse.ArgumentParser()',
    'p.add_argument("--controller")',
    's=p.add_subparsers(dest="operation", required=True)',
    's.add_parser("runtime-adopt-source")',
    'r=s.add_parser("runtime-refresh")',
    'r.add_argument("--previous-sha", required=True)',
    'r.add_argument("--target-sha")',
    'repair_mode=r.add_mutually_exclusive_group()',
    'repair_mode.add_argument("--dry-run", action="store_true")',
    'repair_mode.add_argument("--apply")',
    'a=p.parse_args()',
    `pathlib.Path(${JSON.stringify(marker)}).write_text(json.dumps({'controller':a.controller,'dry_run':a.dry_run}))`,
  ].join('\n'));
  await fs.writeFile(path.join(packagedRoot, 'task_workspace.py'), '# packaged sibling\n');
  return { controller, canonical, packaged, marker };
}

describe('integration runtime-refresh bootstrap routing', () => {
  it('executes the packaged recovery engine when the older controller runtime rejects --dry-run', async () => {
    const { controller, canonical, packaged, marker } = await fixture();
    const args = [
      '--controller', controller, 'runtime-refresh', '--previous-sha', 'a'.repeat(40),
      '--target-sha', 'b'.repeat(40), '--dry-run',
    ];
    const stale = spawnSync('python3', [canonical, ...args], { encoding: 'utf8' });
    expect(stale.status).toBe(2);
    expect(stale.stderr).toContain('unrecognized arguments: --dry-run');

    const selected = await selectIntegrationRuntime(
      controller, 'runtime-refresh', { previousSha: 'a'.repeat(40), dryRun: true }, [packaged],
    );
    expect(selected).toBe(packaged);
    execFileSync('python3', [selected, ...args]);
    expect(await fs.readJson(marker)).toEqual({ controller, dry_run: true });
    expect(await fs.pathExists(path.join(controller, '.juno_task', 'runtime'))).toBe(false);
  });

  it('uses the packaged recovery engine for no-overlap runtime refreshes too', async () => {
    const { controller, canonical, packaged } = await fixture();
    await expect(selectIntegrationRuntime(
      controller, 'runtime-refresh', { previousSha: 'a'.repeat(40) }, [packaged],
    )).resolves.toBe(packaged);
    await expect(selectIntegrationRuntime(controller, 'runtime-doctor', {}, [packaged]))
      .resolves.toBe(canonical);
    await expect(selectIntegrationRuntime(controller, 'status', {}, [packaged]))
      .resolves.toBe(canonical);
  });

  it('selects only the packaged all-in-one source adoption engine', async () => {
    const { controller, packaged } = await fixture();
    await expect(selectIntegrationRuntime(controller, 'runtime-adopt-source', {
      previousSha: 'a'.repeat(40), targetSha: 'b'.repeat(40),
      installPrefix: '/tmp/runtime', output: '/tmp/adoption.json',
    }, [packaged])).resolves.toBe(packaged);
    await fs.writeFile(packaged, '# partial engine\n');
    await expect(selectIntegrationRuntime(controller, 'runtime-adopt-source', {}, [packaged]))
      .rejects.toThrow('incompatible; refusing partial recovery');
  });

  it('fails closed instead of falling back when the packaged recovery protocol is incompatible', async () => {
    const { controller, packaged } = await fixture();
    await fs.writeFile(packaged, '# stale packaged engine\n');
    await expect(selectIntegrationRuntime(
      controller, 'runtime-refresh', { previousSha: 'a'.repeat(40), apply: '/tmp/plan.json' }, [packaged],
    )).rejects.toThrow('incompatible; refusing stale controller fallback');
  });
});
