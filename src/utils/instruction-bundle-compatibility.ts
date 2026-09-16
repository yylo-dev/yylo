import policy from '../templates/instruction-bundle-compatibility.json';

/** Content revisions are not schema changes. Only stable supported-major versions admit. */
export function instructionVersionCompatible(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  // JS $ also matches before a final newline; require the whole matched value.
  return new RegExp(policy.stableVersionPattern).exec(value)?.[0] === value
    && value.split('.')[0] === policy.supportedMajor;
}

export function instructionDeclarationCompatible(schema: unknown, declaration: unknown): boolean {
  if (typeof schema !== 'number' || !policy.manifestSchemas.includes(schema)) return false;
  if (schema === 1) return declaration === undefined || declaration === null;
  if (!declaration || typeof declaration !== 'object' || Array.isArray(declaration)) return false;
  const value = declaration as Record<string, unknown>;
  return Object.keys(value).sort().join(',') === 'schemaVersion,semanticVersion'
    && value.schemaVersion === policy.declarationSchema
    && instructionVersionCompatible(value.semanticVersion);
}

export function assertInstructionVersion(value: unknown): asserts value is string {
  if (!instructionVersionCompatible(value)) {
    throw new Error(`instruction_bundle_incompatible: ${policy.recovery}`);
  }
}

export const INSTRUCTION_IDENTITY_SCHEMA = policy.identitySchema;
