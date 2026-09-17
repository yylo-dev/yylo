import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs-extra';
import { hasSimpleWorkspaceHint, resolveController } from '../controller-resolver.js';
import { checkpointControllerAfterFinalization } from '../controller-checkpoint.js';

vi.mock('../controller-resolver.js', () => ({
  hasSimpleWorkspaceHint: vi.fn(() => false), resolveController: vi.fn(),
}));
vi.mock('fs-extra', () => ({ default: { pathExists: vi.fn(async () => false) } }));

describe('fully nonfatal checkpoint boundary', () => {
  beforeEach(() => {
    vi.mocked(hasSimpleWorkspaceHint).mockReturnValue(false);
    vi.mocked(fs.pathExists).mockResolvedValue(false as never);
    vi.stubEnv('JUNO_CONTROLLER_CHECKPOINT_ACTIVE', '');
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); vi.clearAllMocks(); });

  it.each([0, 7])('does not replace primary exit %s when workspace probing throws', async (exit) => {
    vi.mocked(hasSimpleWorkspaceHint).mockImplementation(() => { throw new Error('probe failed'); });
    const primary = { exit, output: exit ? 'primary error' : 'agent answer' };
    const checkpoint = await checkpointControllerAfterFinalization('/fixture', primary.exit);
    expect(checkpoint).toMatchObject({ attempted: false, ok: false });
    expect(primary).toEqual({ exit, output: exit ? 'primary error' : 'agent answer' });
  });

  it('contains resolver failures even for Simple hints', async () => {
    vi.mocked(hasSimpleWorkspaceHint).mockReturnValue(true);
    vi.mocked(resolveController).mockImplementation(() => { throw new Error('resolution failed'); });
    expect(await checkpointControllerAfterFinalization('/fixture', 0)).toMatchObject({ ok: false });
  });

  it('contains filesystem failures and throwing error rendering/warning sinks', async () => {
    vi.mocked(fs.pathExists).mockRejectedValue({ toString() { throw new Error('render failed'); } });
    vi.mocked(console.error).mockImplementation(() => { throw new Error('closed stderr'); });
    expect(await checkpointControllerAfterFinalization('/fixture', 9)).toMatchObject({
      attempted: false, ok: false, blocker: 'unknown',
    });
  });

  it('keeps missing-script and recursive invocations harmless', async () => {
    expect(await checkpointControllerAfterFinalization('/fixture', 0)).toEqual({ attempted: false, ok: true });
    vi.stubEnv('JUNO_CONTROLLER_CHECKPOINT_ACTIVE', '1');
    expect(await checkpointControllerAfterFinalization('/fixture', 7)).toEqual({ attempted: false, ok: true });
  });
});
