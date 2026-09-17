import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

describe('primary process outcome with a rejecting checkpoint subprocess', () => {
  let root: string;
  beforeEach(async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'checkpoint-primary-'));
    await fs.outputFile(path.join(root, '.juno_task/scripts/controller_checkpoint.py'),
      'import sys\nprint("blocked non-controller paths: [s1034-out.txt]", file=sys.stderr)\nsys.exit(2)\n');
    await fs.writeFile(path.join(root, 's1034-out.txt'), 'preserve me\n');
  });
  afterEach(async () => fs.remove(root));
  it.each([0, 7])('preserves primary output and exit %s', async (code) => {
    const module = pathToFileURL(path.resolve('src/utils/controller-checkpoint.ts')).href;
    const script = `import checkpointModule from ${JSON.stringify(module)};
      const {checkpointControllerAfterFinalization}=checkpointModule;
      const code=${code};
      (code ? process.stderr : process.stdout).write(code ? 'primary agent failure\\n' : 'agent answer\\n');
      const result=await checkpointControllerAfterFinalization(${JSON.stringify(root)},code);
      if(result.ok || !result.attempted) throw new Error('fixture did not exercise checkpoint rejection');
      process.exitCode=code;`;
    const result = spawnSync(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', script], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
      env: { ...process.env, JUNO_TASK_ROOT: root, JUNO_CONTROLLER_CHECKPOINT_ACTIVE: '',
        JUNO_CONTROLLER_BRANCH: '', JUNO_WORKSPACE_ROLE: '' },
    });
    expect(result.status, result.stderr).toBe(code);
    expect(code ? result.stderr : result.stdout).toContain(code ? 'primary agent failure' : 'agent answer');
    expect(result.stderr).toContain('WARNING: Controller checkpoint failed after finalization');
    expect(result.stderr).toContain('s1034-out.txt');
    expect(await fs.readFile(path.join(root, 's1034-out.txt'), 'utf8')).toBe('preserve me\n');
  });
});
