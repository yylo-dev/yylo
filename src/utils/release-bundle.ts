import { createHash } from 'node:crypto';
import semver from 'semver';
import { z } from 'zod';

/** Portable release inputs only. Local installation paths and activation never belong here. */
export const BUNDLE_COMPONENTS = ['cli', 'ledger', 'benchmark', 'skills'] as const;
export type BundleComponent = typeof BUNDLE_COMPONENTS[number];
const sha256 = z.string().regex(/^[a-f0-9]{64}$/);
const exactVersion = z.string().refine(value => semver.valid(value) === value, 'Exact canonical SemVer required');
const https = z.string().url().refine(value => {
  const url = new URL(value);
  return url.protocol === 'https:' && !url.username && !url.password && !url.hash && !url.search;
}, 'Credential-free immutable HTTPS artifact URL required');
const evidenceReference = z.object({ url: https, sha256, bytes: z.number().int().positive().max(64 * 1024) }).strict();
const artifact = z.object({
  version: exactVersion,
  url: https,
  sha256,
  bytes: z.number().int().positive().max(Number.MAX_SAFE_INTEGER),
  source: z.object({ repository: https, commit: z.string().regex(/^[a-f0-9]{40}$/) }).strict(),
}).strict();

export const releaseBundleSchema = z.object({
  schema_version: z.literal('yylo_release_bundle.v1'),
  version: exactVersion,
  controller_generation: z.object({ ordinary_dispatch: z.literal('explicit-only-v1') }).strict(),
  acceptance: evidenceReference.optional(),
  components: z.object({
    cli: artifact.extend({ name: z.literal('@yylo/cli') }),
    ledger: artifact.extend({ name: z.literal('yylo-ledger') }),
    benchmark: artifact.extend({ name: z.literal('@yylo/benchmark') }),
    skills: artifact.extend({ name: z.literal('yylo-skills') }),
  }).strict(),
}).strict().superRefine((bundle, context) => {
  if (bundle.version !== bundle.components.cli.version) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ['version'], message: 'Bundle version must equal CLI release version' });
  }
});
export type ReleaseBundle = z.infer<typeof releaseBundleSchema>;

/** Reviewed Git intent; never an assertion about what this machine has installed. */
export const releasePinSchema = z.object({
  schema_version: z.literal('yylo_release_pin.v1'),
  version: exactVersion,
  manifest: z.object({ url: https, sha256 }).strict(),
}).strict();
export type ReleasePin = z.infer<typeof releasePinSchema>;

const MAX_MANIFEST_BYTES = 64 * 1024;
function parseJson(bytes: Uint8Array): unknown {
  if (bytes.byteLength > MAX_MANIFEST_BYTES) throw new Error('release_manifest_too_large');
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
}
export function parseReleasePin(bytes: Uint8Array): ReleasePin {
  return releasePinSchema.parse(parseJson(bytes));
}

/** The caller must obtain the pin through reviewed project intent/trusted release evidence.
 * A manifest's own digest is not a trust root. No network, writes or package execution occur here.
 */
export function verifyPinnedRelease(bytes: Uint8Array, inputPin: unknown): ReleaseBundle {
  const pin = releasePinSchema.parse(inputPin);
  if (bytes.byteLength > MAX_MANIFEST_BYTES) throw new Error('release_manifest_too_large');
  if (createHash('sha256').update(bytes).digest('hex') !== pin.manifest.sha256) {
    throw new Error('release_manifest_digest_mismatch');
  }
  const bundle = releaseBundleSchema.parse(parseJson(bytes));
  if (bundle.version !== pin.version) throw new Error('release_pin_version_mismatch');
  return bundle;
}

/** Exact artifact bytes, not the executable's self-reported --version response. */
export function verifyBundleArtifact(bundleInput: unknown, component: BundleComponent, bytes: Uint8Array): void {
  const bundle = releaseBundleSchema.parse(bundleInput);
  const expected = bundle.components[component];
  if (!expected) throw new Error('release_component_unknown');
  if (bytes.byteLength !== expected.bytes
      || createHash('sha256').update(bytes).digest('hex') !== expected.sha256) {
    throw new Error(`release_artifact_mismatch:${component}`);
  }
}

/** Check authored package expectations without selecting latest or solving dependencies.
 * A skills compatibility range may be used by old CLI releases, but the bundle always pins
 * exactly one version. This permits explicit preparation before those declarations migrate.
 */
export function verifyBundleDeclarations(bundleInput: unknown, input: {
  cli: { name: string; version: string; yyloLedger: { version: string }; yyloBenchmark: { version: string }; yyloSkills: { version: string }; yyloControllerGeneration?: { ordinaryDispatch: string } };
  benchmark: { name: string; version: string };
}): ReleaseBundle {
  const bundle = releaseBundleSchema.parse(bundleInput);
  if (input.cli.yyloControllerGeneration?.ordinaryDispatch !== bundle.controller_generation.ordinary_dispatch) {
    throw new Error('release_generation_capability_mismatch');
  }
  for (const [role, name, version] of [
    ['cli', input.cli.name, input.cli.version],
    ['benchmark', input.benchmark.name, input.benchmark.version],
  ] as const) {
    if (bundle.components[role].name !== name || bundle.components[role].version !== version) {
      throw new Error(`release_declaration_mismatch:${role}`);
    }
  }
  for (const role of ['ledger', 'benchmark', 'skills'] as const) {
    const declaration = { ledger: input.cli.yyloLedger, benchmark: input.cli.yyloBenchmark, skills: input.cli.yyloSkills }[role];
    if (typeof declaration?.version !== 'string' || !semver.validRange(declaration.version)
        || !semver.satisfies(bundle.components[role].version, declaration.version)) {
      throw new Error(`release_declaration_mismatch:${role}`);
    }
  }
  return bundle;
}

/** Bind test inputs without a manifest/report self-hash cycle. Zod parsing fixes key order.
 * The acceptance reference itself is excluded; all component identities remain included.
 */
export function bundleInputsDigest(input: unknown): string {
  const { version, controller_generation, components } = releaseBundleSchema.parse(input);
  return createHash('sha256').update(JSON.stringify({ version, controller_generation, components })).digest('hex');
}

export function verifyBundleAcceptance(input: unknown, bytes: Uint8Array,
  expectedSource: string, expectedGate: string): void {
  const bundle = releaseBundleSchema.parse(input);
  const reference = bundle.acceptance;
  if (!reference) throw new Error('bundle_acceptance_missing');
  if (bytes.byteLength > MAX_MANIFEST_BYTES || bytes.byteLength !== reference.bytes
      || createHash('sha256').update(bytes).digest('hex') !== reference.sha256) {
    throw new Error('bundle_acceptance_digest_mismatch');
  }
  const report = z.object({
    schema_version: z.literal('yylo_bundle_upgrade_acceptance.v1'),
    outcome: z.literal('passed'),
    coverage: z.literal('four-component-upgrade.v1'),
    bundle_inputs_sha256: sha256,
    source: z.object({ sha: z.string().regex(/^[a-f0-9]{40}$/), dirty: z.literal(false) }).strict(),
    gate_sha256: sha256,
  }).strict().parse(parseJson(bytes));
  if (report.bundle_inputs_sha256 !== bundleInputsDigest(bundle) || report.source.sha !== expectedSource
      || report.gate_sha256 !== expectedGate) throw new Error('bundle_acceptance_inputs_mismatch');
}
