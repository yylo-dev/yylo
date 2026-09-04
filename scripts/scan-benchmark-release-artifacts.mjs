#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { readFile, readdir, stat } from 'node:fs/promises';
import { gunzipSync } from 'node:zlib';
import { resolve, relative } from 'node:path';
import { pathToFileURL } from 'node:url';

const canonicalJson = (value) => {
  if (value === null || typeof value === 'boolean' || typeof value === 'string' || typeof value === 'number') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
};
const sha256 = (value) => `sha256:${createHash('sha256').update(value).digest('hex')}`;
const canonicalHash = (value) => sha256(canonicalJson(value));
const emptyHash = sha256('');

const SECRET_ENV = /(?:TOKEN|SECRET|PASSWORD|AUTH|REGISTRY|API_KEY)/u;
const PRIVATE_PATH_ENV = /^(?:HOME|XDG_(?:CONFIG|CACHE|DATA|STATE)_HOME)$/u;
export const isSensitiveEnvironmentName = (name) => SECRET_ENV.test(name) || PRIVATE_PATH_ENV.test(name);
const DETECTORS = Object.freeze({
  'private-registry': /(?:registry\s*=\s*https?:\/\/(?!registry\.npmjs\.org)|https?:\/\/(?:npm|packages|registry)\.[^\s/]*(?:private|internal)[^\s/]*)/iu,
  'auth-credentials': /(?:https?:\/\/[^\s"']+@|_authToken\s*=|\b(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*["']?[A-Za-z0-9+/_=-]{16,})/iu,
  'candidate-home-xdg': /(?:\b(?:HOME|XDG_(?:CONFIG|CACHE|DATA)_HOME)\s*=\s*\/[^\s]*candidate|\/(?:candidate-home|candidate-xdg)(?:\/|\b))/iu,
  'canonical-controller-route': /(?:\/canonical-controller\/\.juno_task\/|JUNO_TASK_ROOT\s*=\s*\/[^\s]+\/controller(?:\/|\b))/iu,
  'host-paths': /(?:\/Users\/[^/\s]+\/|\/home\/[^/\s]+\/|[A-Za-z]:\\\\Users\\\\[^\\\s]+\\\\)/u,
  'candidate-git-metadata': /(?:gitdir:\s*\/[^\s]*candidate[^\s]*\/\.git|GIT_(?:DIR|WORK_TREE)\s*=\s*\/[^\s]*candidate)/iu,
});
const SYNTHETIC_ARTIFACTS = Object.freeze({
  'private-registry': 'registry=https://npm.private.invalid/juno-release-canary/\n',
  'auth-credentials': '_authToken=juno_release_canary_credential_2d65aa\n',
  'candidate-home-xdg': 'HOME=/candidate-home/release-canary\nXDG_CONFIG_HOME=/candidate-xdg/release-canary\n',
  'canonical-controller-route': 'JUNO_TASK_ROOT=/canonical-controller/.juno_task/release-canary\n',
  'host-paths': '/Users/juno-release-canary/artifact.json\n',
  'candidate-git-metadata': 'gitdir: /candidate-worktree/.git/worktrees/release-canary\n',
});

const forbiddenRuntimeValues = Object.entries(process.env)
  .filter(([name, value]) => value !== undefined && value.length >= 4 && isSensitiveEnvironmentName(name) && !name.startsWith('YYLO_BENCHMARK_RELEASE_'))
  .map(([, value]) => Buffer.from(value));

const files = [];
async function walk(entry) {
  const info = await stat(entry);
  if (info.isSymbolicLink()) throw new Error('leak scan refuses symbolic links');
  if (info.isDirectory()) for (const child of (await readdir(entry)).sort()) await walk(resolve(entry, child));
  else if (info.isFile()) files.push(entry);
  else throw new Error('leak scan refuses non-regular entries');
}

const strictUtf8Bytes = (bytes) => {
  try {
    new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    return true;
  } catch {
    return false;
  }
};

// Maximal strict-UTF-8 runs: text semantics apply exactly to clean text
// regions, and non-UTF-8 bytes are binary covered by byte matching. Testing
// each region independently gives every clean leak native regex
// backtracking, so no greedy, overlapping, first-match, or same-start
// invalid span can mask a later or shorter valid leak.
function* strictUtf8Segments(bytes) {
  const isContinuation = (byte) => (byte & 0xc0) === 0x80;
  let start = 0;
  let index = 0;
  while (index < bytes.length) {
    const byte = bytes[index];
    if (byte < 0x80) {
      index += 1;
      continue;
    }
    let length = 0;
    let lower = 0x80;
    let upper = 0xbf;
    if (byte >= 0xc2 && byte <= 0xdf) {
      length = 2;
    } else if (byte >= 0xe0 && byte <= 0xef) {
      length = 3;
      if (byte === 0xe0) lower = 0xa0;
      if (byte === 0xed) upper = 0x9f;
    } else if (byte >= 0xf0 && byte <= 0xf4) {
      length = 4;
      if (byte === 0xf0) lower = 0x90;
      if (byte === 0xf4) upper = 0x8f;
    } else {
      if (index > start) yield bytes.subarray(start, index);
      index += 1;
      start = index;
      continue;
    }
    let valid = true;
    for (let offset = 1; offset < length; offset += 1) {
      const continuation = bytes[index + offset];
      if (continuation === undefined
          || !isContinuation(continuation)
          || (offset === 1 && (continuation < lower || continuation > upper))) {
        valid = false;
        break;
      }
    }
    if (!valid) {
      if (index > start) yield bytes.subarray(start, index);
      index += 1;
      start = index;
      continue;
    }
    index += length;
  }
  if (bytes.length > start) yield bytes.subarray(start, bytes.length);
}

let bytesScanned = 0;
function detections(bytes) {
  const classes = [];
  for (const segment of strictUtf8Segments(bytes)) {
    const text = segment.toString('utf8');
    for (const [id, detector] of Object.entries(DETECTORS)) {
      if (!classes.includes(id) && detector.test(text)) classes.push(id);
    }
  }
  if (forbiddenRuntimeValues.some((value) => bytes.indexOf(value) >= 0)) classes.push('sensitive-runtime-value');
  return classes;
}

// Minimal fail-closed ustar reader for packed npm tarballs. A packed archive
// mixes strict-UTF-8 text members (README, package.json) with binary members
// (PNG artwork), so text semantics must be decided per member: decoding the
// whole expanded tar as one buffer would let any binary member disable text
// detection for every text member in the archive.
export function tarMembers(expanded) {
  const members = [];
  let offset = 0;
  const field = (start, end) => expanded.toString('latin1', start, end).replace(/\0.*$/u, '');
  const OCTAL_FIELD = /^[0-7]{1,11}[ \0]*$/u;
  // Fail-closed header validation: trust no member boundary until the block
  // proves it is a well-formed ustar header with a matching checksum. A
  // corrupted size field must never be able to absorb and hide a following
  // member from the detector pipeline.
  const validateHeader = (header) => {
    const magic = expanded.toString('latin1', offset + 257, offset + 263);
    if (magic !== 'ustar\0' && magic !== 'ustar  ') {
      throw new Error('leak scan received a malformed expanded tar archive');
    }
    const rawSize = expanded.toString('latin1', offset + 124, offset + 136);
    if (!OCTAL_FIELD.test(rawSize)) {
      throw new Error('leak scan received a malformed expanded tar archive');
    }
    const rawChecksum = expanded.toString('latin1', offset + 148, offset + 156);
    if (!OCTAL_FIELD.test(rawChecksum)) {
      throw new Error('leak scan received a malformed expanded tar archive');
    }
    let recorded;
    try {
      recorded = parseInt(rawChecksum.replace(/[ \0].*$/u, ''), 8);
    } catch {
      throw new Error('leak scan received a malformed expanded tar archive');
    }
    let actual = 0;
    for (let index = 0; index < 512; index += 1) {
      actual += index >= 148 && index < 156 ? 32 : header[index];
    }
    if (!Number.isSafeInteger(recorded) || recorded !== actual) {
      throw new Error('leak scan received a malformed expanded tar archive');
    }
  };
  while (offset + 512 <= expanded.length) {
    const header = expanded.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) {
      // ustar end-of-archive: the second all-zero block must be present and no
      // nonzero byte may follow it (additional all-zero padding is legal).
      const remainder = expanded.subarray(offset + 512);
      if (remainder.length < 512 || !remainder.every((byte) => byte === 0)) {
        throw new Error('leak scan received a truncated expanded tar archive');
      }
      return members;
    }
    validateHeader(header);
    const typeflag = String.fromCharCode(header[156]);
    const size = parseInt(field(offset + 124, offset + 136).trim(), 8);
    if (!Number.isSafeInteger(size) || size < 0 || offset + 512 + size > expanded.length) {
      throw new Error('leak scan received a malformed expanded tar archive');
    }
    const prefix = field(offset + 345, offset + 500);
    const name = field(offset, offset + 100) || `type:${typeflag}`;
    if (!name || name === `type:${typeflag}`) {
      throw new Error('leak scan received a malformed expanded tar archive');
    }
    offset += 512;
    const bytes = expanded.subarray(offset, offset + size);
    const dataEnd = offset + size;
    offset += Math.ceil(size / 512) * 512;
    const framing = Buffer.concat([header, expanded.subarray(dataEnd, offset)]);
    members.push({ name: prefix ? `${prefix}/${name}` : name, typeflag, bytes, framing });
  }
  throw new Error('leak scan received a truncated expanded tar archive');
}
class LeakageDetectionError extends Error {
  constructor(detectedClasses, label) {
    super(`leak classes ${detectedClasses.join(',')} detected in ${label}`);
    this.name = 'LeakageDetectionError';
    this.detectedClasses = Object.freeze([...detectedClasses]);
    this.label = label;
  }
}

export function inspect(bytes, label, count = true) {
  if (count) bytesScanned += bytes.length;
  const found = [...new Set(detections(bytes))].sort();
  if (found.length > 0) throw new LeakageDetectionError(found, label);
}

// Inspect one packed npm tarball with complete framing coverage: exact
// sensitive-value byte matching runs on the compressed and whole expanded
// buffers; the fail-closed ustar reader additionally inspects every member's
// data AND every member's framing bytes (header block plus data-to-boundary
// padding), so every byte of the expanded archive belongs to exactly one
// inspected unit (member data, member framing, or the validated zero
// terminator). Detector-shaped bytes cannot hide in padding or unused
// header-field space.
export function inspectPackedTarball(gzipped, label) {
  const expanded = gunzipSync(gzipped);
  inspect(expanded, `${label} (expanded)`);
  for (const member of tarMembers(expanded)) {
    inspect(member.bytes, `${label} (member ${member.name})`);
    inspect(member.framing, `${label} (framing ${member.name})`);
  }
}

// Execute one bounded, realistic synthetic artifact per required leak class through
// the same rejection path used for release files. Evidence comes from the caught
// production rejection and never retains the canary bytes.
export function runSyntheticLeakageCanaries(sourceTree, commandHash, inspectArtifact = inspect) {
  return Object.entries(SYNTHETIC_ARTIFACTS).map(([checkId, text]) => {
    const bytes = Buffer.from(text);
    const label = `synthetic:${checkId}`;
    let rejection;
    try {
      inspectArtifact(bytes, label, false);
    } catch (error) {
      if (!(error instanceof LeakageDetectionError)) throw error;
      rejection = error;
    }
    if (rejection?.label !== label || !rejection.detectedClasses.includes(checkId)) {
      throw new Error(`leak detector positive control failed: ${checkId}`);
    }
    const detectedClasses = [...rejection.detectedClasses];
    const observed = { detected: true, rejected: true, detected_classes: detectedClasses };
    const output = { synthetic_artifact_hash: sha256(bytes), bytes_scanned: bytes.length, detector: checkId, detected_classes: detectedClasses };
    const stderrHash = sha256(rejection.message);
    const log = { stdout_hash: emptyHash, stderr_hash: stderrHash, combined_hash: canonicalHash({ stdout_hash: emptyHash, stderr_hash: stderrHash }) };
    const core = { schema_version: 'juno_benchmark_release_leakage_result.v1', check_id: checkId, source_tree: sourceTree, command_hash: commandHash, observed, output, output_hash: canonicalHash(output), log, log_hash: canonicalHash(log), passed: true };
    return { ...core, result_hash: canonicalHash(core) };
  });
}

async function main() {
  const roots = process.argv.slice(2);
  if (roots.length !== 4) throw new Error('expected benchmark dist, YYLO dist, and two packed artifact paths');
  const sourceTree = process.env.YYLO_BENCHMARK_RELEASE_SOURCE_TREE;
  const commandHash = process.env.YYLO_BENCHMARK_RELEASE_COMMAND_HASH;
  if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/u.test(sourceTree ?? '') || !/^sha256:[0-9a-f]{64}$/u.test(commandHash ?? '')) {
    throw new Error('leak scan requires a valid source tree and command hash binding');
  }

  for (const root of roots) await walk(resolve(root));
  if (files.length === 0) throw new Error('leak scan received no release artifact files');
  for (const file of files) {
    const bytes = await readFile(file);
    inspect(bytes, relative(process.cwd(), file));
    if (file.endsWith('.tgz')) inspectPackedTarball(bytes, relative(process.cwd(), file));
  }

  const results = runSyntheticLeakageCanaries(sourceTree, commandHash);
  const resultsHash = canonicalHash(results);
  const core = { schema_version: 'juno_benchmark_release_leakage_bundle.v1', source_tree: sourceTree, command_hash: commandHash, results, results_hash: resultsHash };
  process.stdout.write(`${JSON.stringify({ passed: true, files_scanned: files.length, bytes_scanned: bytesScanned, canaries_checked: results.length, sensitive_environment_values_checked: forbiddenRuntimeValues.length, ...core, bundle_hash: canonicalHash(core) })}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) await main();
