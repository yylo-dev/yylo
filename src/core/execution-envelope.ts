import { z } from 'zod';
import type { ExecutionResult } from './engine.js';
import { ExecutionStatus } from './engine.js';
import { normalizeProviderObservations } from './provider-observations.js';

export const JUNO_EXECUTION_ENVELOPE_VERSION = 'juno_execution_envelope.v1' as const;

export const junoExecutionEnvelopeSchema = z.object({
  schema_version: z.literal(JUNO_EXECUTION_ENVELOPE_VERSION),
  command: z.object({ name: z.literal('managed.run'), version: z.literal(1) }).strict(),
  status: z.enum(['success', 'failure', 'timeout', 'cancelled']),
  session_id: z.string().min(1).nullable(),
  provider: z.string().min(1).nullable(),
  model: z.string().min(1).nullable(),
  juno_version: z.string().min(1),
  error: z.object({ code: z.string().min(1), message: z.string(), exit_code: z.number().int().positive() }).strict().nullable(),
  cost: z.discriminatedUnion('completeness', [
    z.object({ completeness: z.literal('complete'), usd: z.number().finite().nonnegative() }).strict(),
    z.object({ completeness: z.literal('partial'), usd: z.number().finite().nonnegative() }).strict(),
    z.object({ completeness: z.literal('unavailable'), usd: z.null() }).strict(),
    z.object({ completeness: z.literal('not_applicable'), usd: z.null() }).strict(),
  ]),
}).strict();

export type JunoExecutionEnvelope = z.infer<typeof junoExecutionEnvelopeSchema>;

/** Extract the final assistant result for a caller-owned evidence channel. */
export function extractJunoExecutionResponse(result: ExecutionResult): string {
  const content = result.iterations.at(-1)?.toolResult?.content;
  if (typeof content !== 'string') return '';
  try {
    const payload = JSON.parse(content) as Record<string, unknown>;
    if (payload['type'] === 'result' && Object.prototype.hasOwnProperty.call(payload, 'result')) {
      const value = payload['result'];
      if (typeof value === 'string') return value;
      if (value === null || value === undefined) return '';
      return JSON.stringify(value);
    }
  } catch { /* ordinary unstructured result */ }
  return content;
}

/** Build the sole public machine execution contract from backend observations, never assistant prose. */
export function buildJunoExecutionEnvelope(result: ExecutionResult, junoVersion: string): JunoExecutionEnvelope {
  const normalized = normalizeProviderObservations(result);
  const identities = normalized.observations.filter((item) => item.provider !== null && item.resolved_model !== null);
  const providers = new Set(identities.map((item) => item.provider));
  const models = new Set(identities.map((item) => item.resolved_model));
  const sessions = new Set(normalized.observations.map((item) => item.session_id).filter((item): item is string => item !== null));
  const amount = normalized.estimated_cost.amount;
  const cost = amount === null
    ? { completeness: 'unavailable' as const, usd: null }
    : { completeness: normalized.estimated_cost.status === 'available' ? 'complete' as const : 'partial' as const, usd: amount };
  const status = result.status === ExecutionStatus.COMPLETED ? 'success'
    : result.status === ExecutionStatus.TIMEOUT ? 'timeout'
      : result.status === ExecutionStatus.CANCELLED ? 'cancelled' : 'failure';
  return junoExecutionEnvelopeSchema.parse({
    schema_version: JUNO_EXECUTION_ENVELOPE_VERSION,
    command: { name: 'managed.run', version: 1 },
    status,
    session_id: sessions.size === 1 ? [...sessions][0] : null,
    provider: providers.size === 1 ? [...providers][0] : null,
    model: models.size === 1 ? [...models][0] : null,
    juno_version: junoVersion,
    error: status === 'success' ? null : {
      code: `EXECUTION_${status.toUpperCase()}`,
      message: `Managed execution ended with status ${status}`,
      exit_code: status === 'cancelled' ? 130 : status === 'timeout' ? 124 : 1,
    },
    cost,
  });
}
