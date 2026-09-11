import { describe, expect, it } from 'vitest';
import { buildJunoExecutionEnvelope, extractJunoExecutionResponse } from '../execution-envelope.js';
import { ExecutionStatus, type ExecutionResult } from '../engine.js';

function result(payload: Record<string, unknown>, status = ExecutionStatus.COMPLETED): ExecutionResult {
  const now = new Date('2026-01-01T00:00:00.000Z');
  return { request: { instruction: 'fixture', subagent: 'pi', backend: 'shell', workingDirectory: '/tmp', maxIterations: 1 },
    status, startTime: now, endTime: now, duration: 0,
    iterations: [{ iterationNumber: 1, success: status === ExecutionStatus.COMPLETED, startTime: now, endTime: now, duration: 0,
      toolResult: { success: true, content: JSON.stringify({ type: 'result', ...payload }), metadata: { structuredOutput: true } }, progressEvents: [] }],
    statistics: { totalIterations: 1, successfulIterations: 1, failedIterations: 0, averageIterationDuration: 0, totalExecutionTime: 0 },
    sessionContext: { sessionId: 'fixture', startTime: now, lastActivity: now, iterationCount: 1, totalTokens: 0 }, progressEvents: [] } as unknown as ExecutionResult;
}

describe('public Juno execution envelope', () => {
  it.each([
    ['openai-codex', 'gpt-5.6-sol'], ['openai-codex', 'gpt-5.6-mini'],
    ['openai-codex', 'gpt-5.6-luna'], ['zai', 'glm-5.2'],
  ])('keeps separately observed %s/%s identity', (provider, model) => {
    expect(buildJunoExecutionEnvelope(result({ session_id: 'S', provider, model, total_cost_usd: 0 }), '2.1.3')).toEqual({
      schema_version: 'juno_execution_envelope.v1', command: { name: 'managed.run', version: 1 },
      status: 'success', session_id: 'S', provider, model, juno_version: '2.1.3', error: null,
      cost: { completeness: 'complete', usd: 0 },
    });
  });

  it('distinguishes missing cost and execution failure', () => {
    expect(buildJunoExecutionEnvelope(result({ session_id: 'S', provider: 'zai', model: 'glm-5.2' }), '2.1.3').cost)
      .toEqual({ completeness: 'unavailable', usd: null });
    expect(buildJunoExecutionEnvelope(result({}, ExecutionStatus.FAILED), '2.1.3').status).toBe('failure');
  });

  it('keeps assistant prose outside the public metadata envelope', () => {
    const execution = result({ session_id: 'S', provider: 'zai', model: 'glm-5.2', result: 'answer' });
    expect(extractJunoExecutionResponse(execution)).toBe('answer');
    expect(buildJunoExecutionEnvelope(execution, '2.1.3')).not.toHaveProperty('response');
  });
});
