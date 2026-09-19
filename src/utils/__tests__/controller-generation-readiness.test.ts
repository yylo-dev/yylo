import { beforeEach, describe, expect, it, vi } from 'vitest';
import { controllerGenerationReadiness } from '../controller-generation-readiness.js';
const mocks = vi.hoisted(() => ({ admit: vi.fn(), assess: vi.fn(), context: vi.fn(), release: vi.fn() }));
vi.mock('../controller-generation-startup.js', () => ({ admitControllerCommand: mocks.admit, assessControllerGeneration: mocks.assess }));
vi.mock('../controller-generation-migration.js', () => ({ generationDiagnosticContext: mocks.context }));
beforeEach(() => {
  vi.resetAllMocks();
  mocks.admit.mockResolvedValue({ assessment: { disposition: 'retained' }, release: mocks.release });
  mocks.assess.mockResolvedValue({ disposition: 'ready' });
  mocks.context.mockResolvedValue({ source: { status: 'pass', sha: 'a'.repeat(40), remote_verified: false }, launchers: { status: 'pass' } });
});
describe('optional controller readiness', () => {
  it('keeps safe retained execution separate from an available candidate and hides plans', async () => {
    mocks.assess.mockResolvedValue({ disposition: 'migration_required', plan: { before: 'PRIVATE' } });
    const report = await controllerGenerationReadiness('/controller', '/package');
    expect(report.disposition).toBe('ready');
    expect(report.checks.active.status).toBe('pass');
    expect(report.checks.candidate.status).toBe('available');
    expect(JSON.stringify(report)).not.toContain('PRIVATE');
    expect(report.checks.source.remote_verified).toBe(false);
    expect(mocks.release).toHaveBeenCalledOnce();
  });
  it('does not certify an unsafe active runtime from candidate readiness', async () => {
    mocks.admit.mockRejectedValue(new Error('incompatible source'));
    const report = await controllerGenerationReadiness('/controller', '/package');
    expect(report.disposition).toBe('action_required');
    expect(report.checks.active.reason).toContain('incompatible source');
    expect(report.checks.candidate.status).toBe('current');
  });
  it.each(['source', 'launchers'])('requires a verified %s check', async check => {
    const context = await mocks.context();
    context[check] = { status: 'action_required', reason: 'missing or unverifiable' };
    const report = await controllerGenerationReadiness('/controller', '/package');
    expect(report.disposition).toBe('action_required');
    expect(report.checks.active.status).toBe('pass');
  });
  it('preserves bounded-cache refusal without declaring the active runtime unsafe', async () => {
    mocks.assess.mockResolvedValue({ disposition: 'refused', detail: 'package_provenance_bounds' });
    const report = await controllerGenerationReadiness('/controller', '/package');
    expect(report.disposition).toBe('action_required');
    expect(report.checks.active.status).toBe('pass');
    expect(report.checks.candidate.reason).toBe('package_provenance_bounds');
  });
  it('releases its reader on diagnostic failure', async () => {
    mocks.context.mockRejectedValue(new Error('failed diagnostic'));
    const report = await controllerGenerationReadiness('/controller', '/package');
    expect(report.disposition).toBe('action_required');
    expect(report.checks.launchers.reason).toContain('failed diagnostic');
    expect(mocks.release).toHaveBeenCalledOnce();
  });
});
