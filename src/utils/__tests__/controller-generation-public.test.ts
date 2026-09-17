import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { it } from 'vitest';

it('public built CLI migrates offline, starts tasks, retains active pins and preserves fallback exits', () => {
  execFileSync('npm', ['run', 'build'], { timeout: 180_000, maxBuffer: 4 * 1024 * 1024 });
  execFileSync('python3', [path.resolve('src/templates/maintenance/tests/test_controller_generation_dispatch.py')],
    { timeout: 300_000, maxBuffer: 4 * 1024 * 1024 });
}, 490_000);
