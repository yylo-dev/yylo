import { generationDiagnosticContext, type GenerationDiagnosticContext } from './controller-generation-migration.js';
import { admitControllerCommand, assessControllerGeneration } from './controller-generation-startup.js';

/** Optional diagnostics, never an ordinary-startup prerequisite or admission proof. */
export async function controllerGenerationReadiness(controller: string, packageRoot: string) {
  let release: (() => Promise<void>) | undefined;
  let active: { status: 'pass' | 'action_required'; reason: string };
  try {
    const admission = await admitControllerCommand(controller, packageRoot);
    release = admission.release;
    active = { status: 'pass', reason: admission.assessment.disposition === 'retained'
      ? 'Authenticated retained active runtime; invoking package need not equal active runtime.'
      : 'Authenticated active runtime, managed scripts, inventory, source compatibility and applicable task pins.' };
  } catch (error) {
    active = { status: 'action_required', reason: String(error) };
  }
  try {
    let context: GenerationDiagnosticContext;
    try {
      context = await generationDiagnosticContext(controller, packageRoot);
    } catch (error) {
      context = { source: { status: 'action_required', reason: String(error) },
        launchers: { status: 'action_required', reason: String(error) } };
    }
    const assessment = await assessControllerGeneration(controller, packageRoot);
    // Never publish a migration plan: it contains private preimage bytes.
    const candidate = assessment.disposition === 'migration_required'
      ? { status: 'available', reason: 'Authenticated candidate can be explicitly activated; availability does not make the active runtime unsafe.' }
      : assessment.disposition === 'ready'
        ? { status: 'current', reason: 'Invoking package assessment is ready.' }
        : { status: 'action_required', reason: assessment.disposition === 'retained' ? assessment.reason
          : assessment.disposition === 'refused' ? assessment.detail : 'Interrupted generation transaction requires explicit recovery.' };
    return {
      schema_version: 'yylo_controller_readiness.v1', controller,
      disposition: active.status === 'pass' && context.source.status === 'pass'
        && context.launchers.status === 'pass' && candidate.status !== 'action_required' ? 'ready' : 'action_required',
      checks: { active, source: context.source, launchers: context.launchers, candidate },
      safeNextAction: 'Review each check separately. Preserve prior runtimes and task pins. Use explicit generation maintenance for activation or recovery; review launcher installation evidence for missing/mixed commands. Git pull is not activation.',
    };
  } finally {
    await release?.();
  }
}
