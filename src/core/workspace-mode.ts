import { z } from 'zod';

/** One persisted mode authority; registration must still be validated by the resolver. */
export const WorkspaceModeSchema = z.discriminatedUnion('mode', [
  z.object({
    mode: z.literal('metadata-only'),
    policy: z.literal('.juno_task/config/metadata-controller.json'),
  }).strict(),
  z.object({
    mode: z.literal('simple'),
    version: z.literal(1),
  }).strict(),
]);

export type WorkspaceMode = z.infer<typeof WorkspaceModeSchema>;
export type WorkspaceCapability =
  | 'diagnostics'
  | 'local-agent'
  | 'ledger'
  | 'local-task-bookkeeping'
  | 'managed-task'
  | 'managed-merge'
  | 'managed-integration';

/** Mode-level eligibility only, not registration, dependency readiness or write authority. */
export function workspaceCapabilities(value: unknown): readonly WorkspaceCapability[] {
  // Absence is deliberately not an implicit Simple or managed registration.
  if (value === undefined) return Object.freeze(['diagnostics'] as WorkspaceCapability[]);
  const workspace = WorkspaceModeSchema.parse(value);
  return Object.freeze(workspace.mode === 'simple'
    ? ['diagnostics', 'local-agent', 'ledger', 'local-task-bookkeeping'] as WorkspaceCapability[]
    : ['diagnostics', 'ledger', 'managed-task', 'managed-merge', 'managed-integration'] as WorkspaceCapability[]);
}

/** Validate the mode shape; runtime callers must additionally resolve persisted authority. */
export function assertWorkspaceStartupSupported(value: unknown): void {
  if (value === undefined) return;
  WorkspaceModeSchema.parse(value);
}
