import { createHash } from 'node:crypto';
import { chmod, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { gzipSync } from 'node:zlib';
import { execa } from 'execa';
import { afterEach, describe, expect, it } from 'vitest';
import { useSharedHeavyWorkloadLock } from '../../test-utils/resource-lock.js';
import packageJson from '../../../package.json';
import { inspect, inspectPackedTarball, isSensitiveEnvironmentName, runSyntheticLeakageCanaries } from '../../../scripts/scan-benchmark-release-artifacts.mjs';
import { MAX_BENCHMARK_RELEASE_COMMAND_TIMEOUT_MS, runBoundedReleaseCommand } from '../../../scripts/bounded-release-command.mjs';
import {
  BENCHMARK_VERSION_RANGE,
  BenchmarkDelegateError,
  discoverBenchmarkExecutable,
  invokeBenchmark,
} from '../commands/benchmark.js';

const fixtures: string[] = [];
const projectRoot = path.resolve(__dirname, '../../..');
const leakageSourceTree = '2'.repeat(40);
const leakageCommandHash = `sha256:${'3'.repeat(64)}`;
const requiredLeakageClasses = [
  'private-registry', 'auth-credentials', 'candidate-home-xdg',
  'canonical-controller-route', 'host-paths', 'candidate-git-metadata',
];
const sha256 = (value: string) => `sha256:${createHash('sha256').update(value).digest('hex')}`;
const requiredBenchmarkVersion = packageJson.yyloBenchmark.version;

useSharedHeavyWorkloadLock('benchmark clean-source release artifact smoke');

async function fixture(): Promise<{ root: string; bin: string; record: string }> {
  const root = await mkdtemp(path.join(os.tmpdir(), 'yylo-benchmark-delegate-'));
  fixtures.push(root);
  const binDirectory = path.join(root, 'bin');
  const executable = path.join(binDirectory, 'yylo-benchmark');
  const record = path.join(root, 'record.json');
  await import('node:fs/promises').then(({ mkdir }) => mkdir(binDirectory));
  await writeFile(executable, `#!/usr/bin/env node
const fs = require('node:fs');
if (process.argv[2] === '--version') {
  if (process.env.FAKE_VERSION_WAIT) setInterval(() => {}, 1000);
  else {
    process.stdout.write(process.env.FAKE_BENCHMARK_VERSION || 'yylo-benchmark ${requiredBenchmarkVersion}');
    process.exit(Number(process.env.FAKE_VERSION_EXIT || 0));
  }
}
fs.writeFileSync(process.env.FAKE_RECORD, JSON.stringify({
  argv: process.argv.slice(2), cwd: process.cwd(), marker: process.env.DELEGATE_MARKER,
  preflightPresent: Object.prototype.hasOwnProperty.call(process.env, 'YYLO_PREFLIGHT_ONLY')
}));
process.stdout.write('delegate stdout\\n');
process.stderr.write('delegate stderr\\n');
process.exit(Number(process.env.FAKE_EXIT || 0));
`);
  await chmod(executable, 0o755);
  return { root, bin: binDirectory, record };
}

afterEach(async () => {
  await Promise.all(fixtures.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe('benchmark release leakage canaries', () => {
  it('scans private path and credential variables without treating generic XDG session metadata as a secret', () => {
    for (const name of ['HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'NPM_TOKEN', 'API_KEY']) {
      expect(isSensitiveEnvironmentName(name), name).toBe(true);
    }
    for (const name of ['XDG_SESSION_CLASS', 'XDG_CURRENT_DESKTOP', 'PATH']) {
      expect(isSensitiveEnvironmentName(name), name).toBe(false);
    }
  });
  it('runs every canary through the production rejection path and hashes its actual failure', () => {
    const failures = new Map<string, Error & { detectedClasses: string[] }>();
    const calls: Array<{ label: string; count: boolean }> = [];
    const productionRejection = (bytes: Buffer, label: string, count: boolean) => {
      calls.push({ label, count });
      try {
        return inspect(bytes, label, count);
      } catch (error) {
        failures.set(label, error as Error & { detectedClasses: string[] });
        throw error;
      }
    };

    const results = runSyntheticLeakageCanaries(leakageSourceTree, leakageCommandHash, productionRejection);

    expect(results.map((result: { check_id: string }) => result.check_id)).toEqual(requiredLeakageClasses);
    expect(calls).toEqual(requiredLeakageClasses.map((checkId) => ({ label: `synthetic:${checkId}`, count: false })));
    for (const result of results) {
      const failure = failures.get(`synthetic:${result.check_id}`)!;
      expect(failure).toBeInstanceOf(Error);
      expect(result.passed).toBe(true);
      expect(result.observed).toEqual({ detected: true, rejected: true, detected_classes: failure.detectedClasses });
      expect(result.output.detected_classes).toEqual(failure.detectedClasses);
      expect(result.log.stderr_hash).toBe(sha256(failure.message));
    }
  });

  it('fails closed when the production rejection path is bypassed', () => {
    expect(() => runSyntheticLeakageCanaries(leakageSourceTree, leakageCommandHash, () => undefined))
      .toThrow(/positive control failed: private-registry/u);
  });

  it('does not pass a canary rejected for a different leakage class', () => {
    const rejectAsCredentials = (_bytes: Buffer, label: string, count: boolean) =>
      inspect(Buffer.from('_authToken=juno_release_wrong_class_credential\n'), label, count);
    expect(() => runSyntheticLeakageCanaries(leakageSourceTree, leakageCommandHash, rejectAsCredentials))
      .toThrow(/positive control failed: private-registry/u);
  });
});

function ustarEntry(name: string, data: Buffer): Buffer {
  const header = Buffer.alloc(512);
  header.write(name.slice(0, 99), 0, 100, 'utf8');
  header.write('0000644\0', 100, 8, 'ascii');
  header.write('0000000\0', 108, 8, 'ascii');
  header.write('0000000\0', 116, 8, 'ascii');
  header.write(`${data.length.toString(8).padStart(11, '0')} `, 124, 12, 'ascii');
  header.write('00000000000 ', 136, 12, 'ascii');
  header.write('0', 156, 1, 'ascii');
  header.write('ustar\0', 257, 6, 'ascii');
  header.write('00', 263, 2, 'ascii');
  header.fill(' ', 148, 156);
  const checksum = header.reduce((sum, byte) => (sum + byte) & 0o7777777, 0);
  header.write(`${checksum.toString(8).padStart(6, '0')}\0 `, 148, 8, 'ascii');
  const padding = Buffer.alloc((512 - (data.length % 512)) % 512);
  return Buffer.concat([header, data, padding]);
}

function packedTarball(entries: Array<{ name: string; data: Buffer }>, terminator: Buffer = Buffer.alloc(1024)): Buffer {
  return gzipSync(Buffer.concat([...entries.map((entry) => ustarEntry(entry.name, entry.data)), terminator]));
}

describe('packed tarball leakage scanning', () => {
  const binaryArtwork = Buffer.concat([
    Buffer.from([0xff, 0xfe, 0x00, 0x40, 0x89, 0x50, 0x4e, 0x47]),
    Buffer.from('http://cv.iptc.org/non-utf8-binary-provenance\n', 'latin1'),
    Buffer.from([0xc3, 0x28, 0xff, 0x10]),
  ]);

  it('detects a text leak in a mixed binary/text archive instead of letting the binary member disable text detection', () => {
    const tarball = packedTarball([
      { name: 'package/artwork.bin', data: binaryArtwork },
      { name: 'package/leak.txt', data: Buffer.from('registry=https://npm.private.invalid/repro/\n') },
    ]);
    expect(() => inspectPackedTarball(tarball, 'mixed.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['private-registry']) }),
    );
  });

  it('inspects every member at its own header offset in multi-member archives', () => {
    // Distinct member sizes pin per-member header parsing: a reader that
    // reuses one header's size field desyncs and must fail closed here.
    const members = [
      { name: 'package/assets/yylo-logo-square-neon-green.png', data: binaryArtwork },
      { name: 'package/large.bin', data: Buffer.concat(Array.from({ length: 3 }, () => binaryArtwork)) },
      { name: 'package/docs/guide.md', data: Buffer.from('# Guide\n\nSafe documentation text.\n'.repeat(12)) },
      { name: 'package/hidden-leak.txt', data: Buffer.from('_authToken=juno_release_canary_credential_2d65aa\n') },
    ];
    const tarball = packedTarball(members);
    expect(() => inspectPackedTarball(tarball, 'multi.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['auth-credentials']) }),
    );
  });

  it('accepts the binary artwork when the remaining text members are clean', () => {
    const tarball = packedTarball([
      { name: 'package/assets/yylo-logo-square-neon-green.png', data: binaryArtwork },
      { name: 'package/package.json', data: Buffer.from('{"name":"@yylo/cli","private":false}\n') },
    ]);
    expect(() => inspectPackedTarball(tarball, 'artwork.tgz')).not.toThrow();
  });

  it('fails closed on a malformed expanded tar archive', () => {
    expect(() => inspectPackedTarball(gzipSync(Buffer.from('not-a-tar-archive'.repeat(64))), 'broken.tgz'))
      .toThrow(/malformed expanded tar/u);
  });

  it('fails closed when the second ustar end-of-archive block is missing', () => {
    const tarball = packedTarball(
      [{ name: 'package/artwork.bin', data: binaryArtwork }],
      Buffer.alloc(512),
    );
    expect(() => inspectPackedTarball(tarball, 'truncated.tgz')).toThrow(/truncated expanded tar/u);
  });

  it('fails closed when nonzero bytes follow the first ustar end-of-archive block', () => {
    const tarball = packedTarball(
      [{ name: 'package/artwork.bin', data: binaryArtwork }],
      Buffer.concat([Buffer.alloc(1024), Buffer.from('X'.repeat(64))]),
    );
    expect(() => inspectPackedTarball(tarball, 'trailing.tgz')).toThrow(/truncated expanded tar/u);
  });

  it('detects detector-shaped trailing bytes after the ustar terminator', () => {
    const tarball = packedTarball(
      [{ name: 'package/artwork.bin', data: binaryArtwork }],
      Buffer.concat([Buffer.alloc(1024), Buffer.from('registry=https://npm.private.invalid/trailing/')]),
    );
    expect(() => inspectPackedTarball(tarball, 'trailing-leak.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['private-registry']) }),
    );
  });

  it('accepts extra all-zero padding blocks after the ustar terminator', () => {
    const tarball = packedTarball(
      [{ name: 'package/assets/yylo-logo-square-neon-green.png', data: binaryArtwork }],
      Buffer.alloc(2048),
    );
    expect(() => inspectPackedTarball(tarball, 'padded.tgz')).not.toThrow();
  });

  it('cannot hide a leaking member behind a corrupted size header', () => {
    // Valid members: binary artwork, then a detector-shaped text member.
    const members = [
      { name: 'package/artwork.bin', data: binaryArtwork },
      { name: 'package/leak.txt', data: Buffer.from('registry=https://npm.private.invalid/repro/\n') },
    ];
    const expanded = Buffer.concat([
      ...members.map((entry) => ustarEntry(entry.name, entry.data)),
      Buffer.alloc(1024),
    ]);
    // Corrupt the first header's size to span both members while keeping the
    // stale checksum. The whole-buffer scan detects the hidden leak even
    // before header validation rejects the forged boundary.
    const spanned = 512 + binaryArtwork.length + 512 + 42;
    expanded.write(`${spanned.toString(8).padStart(11, '0')} `, 124, 12, 'ascii');
    expect(() => inspectPackedTarball(gzipSync(expanded), 'corrupt-size-leak.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['private-registry']) }),
    );
  });

  it('rejects a corrupted size header on structural grounds alone', () => {
    const members = [
      { name: 'package/artwork.bin', data: binaryArtwork },
      { name: 'package/notes.txt', data: Buffer.from('plain absorbed filler text without any leak shape\n') },
    ];
    const expanded = Buffer.concat([
      ...members.map((entry) => ustarEntry(entry.name, entry.data)),
      Buffer.alloc(1024),
    ]);
    const spanned = 512 + binaryArtwork.length + 512 + 48;
    expanded.write(`${spanned.toString(8).padStart(11, '0')} `, 124, 12, 'ascii');
    expect(() => inspectPackedTarball(gzipSync(expanded), 'corrupt-size.tgz'))
      .toThrow(/malformed expanded tar/u);
  });

  it('rejects a nonzero block without ustar magic even with a self-consistent checksum', () => {
    const expanded = Buffer.concat([
      (() => {
        const header = Buffer.alloc(512, 0x20);
        header.write('not-ustar', 0, 9, 'ascii');
        return header;
      })(),
      Buffer.alloc(1024),
    ]);
    expect(() => inspectPackedTarball(gzipSync(expanded), 'no-magic.tgz'))
      .toThrow(/malformed expanded tar/u);
  });

  it('rejects detector-shaped text hidden in member padding even beside invalid bytes', () => {
    // Valid binary member whose data ends mid-block; hide a registry leak in
    // the padding together with a stray non-UTF-8 byte in the same framing
    // unit. Region-wide strict decoding would skip the whole unit; span-level
    // validation must still catch the clean leak span.
    const data = binaryArtwork.subarray(0, 100);
    const entry = ustarEntry('package/artwork.bin', data);
    const padding = entry.subarray(512 + data.length);
    padding.write('registry=https://npm.private.invalid/padding/', 0, 45, 'ascii');
    padding[45 + 4] = 0xff;
    const tarball = gzipSync(Buffer.concat([entry, Buffer.alloc(1024)]));
    expect(() => inspectPackedTarball(tarball, 'padding-leak.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['private-registry']) }),
    );
  });

  it('rejects an invalid first match without masking a later valid leak of the same detector', () => {
    // First padding region carries a credential-shaped URL followed by binary
    // bytes (invalid span, skipped); a second member carries a clean valid
    // _authToken leak that must still be detected.
    const noisy = ustarEntry('package/artwork.bin', binaryArtwork.subarray(0, 100));
    const noisyPadding = noisy.subarray(512 + 100);
    noisyPadding.write('https://user:', 0, 13, 'ascii');
    noisyPadding[13] = 0xff;
    noisyPadding[14] = 0xfe;
    noisyPadding[15] = 0x40; // '@' shapes a credentials URL across binary bytes
    const leakMember = ustarEntry(
      'package/leak.txt',
      Buffer.from('_authToken=juno_release_canary_credential_2d65aa\n'),
    );
    const tarball = gzipSync(Buffer.concat([noisy, leakMember, Buffer.alloc(1024)]));
    expect(() => inspectPackedTarball(tarball, 'masked-first-match.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['auth-credentials']) }),
    );
  });

  it('rejects a valid leak overlapped or masked by invalid greedy matches of the same detector', () => {
    // One framing region: an invalid greedy credential-shaped URL starts
    // earlier and would swallow the later clean credential URL inside its
    // span; segmentation gives the clean region native backtracking so both
    // overlapping and same-start shorter valid matches stay live.
    const data = Buffer.from('clean binary artwork bytes\n');
    const entry = ustarEntry('package/artwork.bin', data);
    const padding = entry.subarray(512 + data.length);
    padding.write('https://a', 0, 9, 'ascii');
    padding[9] = 0xff;
    padding[10] = 0x40; // '@' ends an invalid greedy span
    padding.write('https://user:secretpw@npm.private.invalid/x', 16, 42, 'ascii');
    padding[42 + 16] = 0xff;
    padding[43 + 16] = 0xfe;
    padding[44 + 16] = 0x40; // later '@' would extend a greedy span across binary bytes
    const tarball = gzipSync(Buffer.concat([entry, Buffer.alloc(1024)]));
    expect(() => inspectPackedTarball(tarball, 'overlapped-leak.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['auth-credentials']) }),
    );
  });

  it('keeps accepting the binary artwork whose detector spans cover non-UTF-8 bytes', () => {
    // The artwork embeds a credential-shaped URL followed by binary bytes;
    // its regex span is not strict UTF-8, so byte-oriented span validation
    // must not turn it into a false positive.
    const tarball = packedTarball([
      { name: 'package/assets/yylo-logo-square-neon-green.png', data: binaryArtwork },
      { name: 'package/README.md', data: Buffer.from('# YYLO\n\nClean release documentation.\n') },
    ]);
    expect(() => inspectPackedTarball(tarball, 'artwork.tgz')).not.toThrow();
  });

  it('rejects detector-shaped text hidden after the name NUL inside a header', () => {
    const data = Buffer.from('clean text member\n');
    const entry = ustarEntry('package/a.txt', data);
    // Unused name-field bytes after the terminating NUL are framing bytes and
    // must not become an uninspected hiding place. Recompute the checksum so
    // the malicious header is structurally valid and only framing inspection
    // can catch it.
    entry.write('registry=https://npm.private.invalid/name/', 40, 43, 'ascii');
    entry.fill(' ', 148, 156);
    let checksum = 0;
    for (let index = 0; index < 512; index += 1) checksum = (checksum + entry[index]) & 0o7777777;
    entry.write(`${checksum.toString(8).padStart(6, '0')}\0 `, 148, 8, 'ascii');
    const tarball = gzipSync(Buffer.concat([entry, Buffer.alloc(1024)]));
    expect(() => inspectPackedTarball(tarball, 'name-leak.tgz')).toThrowError(
      expect.objectContaining({ name: 'LeakageDetectionError', detectedClasses: expect.arrayContaining(['private-registry']) }),
    );
  });
});

describe('benchmark delegate', () => {
  it('hard-kills a genuinely hung bounded release child and refuses an unbounded budget', () => {
    const result = runBoundedReleaseCommand(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { timeout: 50 });
    expect(result.error).toMatchObject({ code: 'ETIMEDOUT' });
    expect(result.signal).toBe('SIGKILL');
    expect(() => runBoundedReleaseCommand(process.execPath, ['--version'], {
      timeout: MAX_BENCHMARK_RELEASE_COMMAND_TIMEOUT_MS + 1,
    })).toThrow(/timeout must be an integer/u);
  });

  it('packs, installs, and verifies standalone/delegate equivalence from tracked sources', async () => {
    const result = await execa(process.execPath, ['scripts/verify-benchmark-release-artifacts.mjs'], {
      cwd: projectRoot,
      reject: false,
      // Setup/pack time is bounded separately from the exact 300s coverage
      // child deadline encoded in RELEASE_VERIFICATION_COMMANDS.
      timeout: 600_000,
    });
    expect(result.exitCode, result.stderr).toBe(0);
    expect(result.stdout).toContain('benchmark release artifact smoke passed');
  }, 610_000);

  it('discovers only PATH executables and preserves argument order, cwd, and caller environment', async () => {
    const { root, bin, record } = await fixture();
    const cwd = path.join(root, 'working directory');
    await import('node:fs/promises').then(({ mkdir }) => mkdir(cwd));
    const env = {
      ...process.env,
      PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`,
      FAKE_RECORD: record,
      DELEGATE_MARKER: 'exact value',
      YYLO_PREFLIGHT_ONLY: 'must-not-leak',
    };
    const args = ['plan', '--task', 'T 1', '--models', ':mini,:sol', '--dry-run'];

    const result = await invokeBenchmark(args, { cwd, env });
    const observed = JSON.parse(await readFile(record, 'utf8'));

    expect(result).toEqual({ code: 0, signal: null });
    expect(observed).toEqual({
      argv: args,
      cwd: await realpath(cwd),
      marker: 'exact value',
      preflightPresent: true,
    });
  });

  it('returns the canonical executable nonzero status unchanged', async () => {
    const { root, bin, record } = await fixture();
    const result = await invokeBenchmark(['run', '--dry-run'], {
      cwd: root,
      env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`, FAKE_RECORD: record, FAKE_EXIT: '47' },
    });
    expect(result).toEqual({ code: 47, signal: null });
  });

  it('resolves relative PATH entries from the delegated working directory', async () => {
    const { root, record } = await fixture();
    const env = {
      ...process.env,
      PATH: `bin${path.delimiter}${process.env.PATH ?? ''}`,
      FAKE_RECORD: record,
    };
    const result = await invokeBenchmark(['plan'], { cwd: root, env });
    expect(result).toEqual({ code: 0, signal: null });
  });

  it('fails closed with an actionable missing-executable diagnostic', () => {
    expect(() => discoverBenchmarkExecutable({ PATH: '' })).toThrowError(BenchmarkDelegateError);
    try {
      discoverBenchmarkExecutable({ PATH: '' });
    } catch (error) {
      expect(error).toMatchObject({ exitCode: 127 });
      expect(String(error)).toContain('independently installed');
      expect(String(error)).toContain(BENCHMARK_VERSION_RANGE);
    }
  });

  it.each(['yylo-benchmark 0.1.0', 'yylo-benchmark 1.0.0', 'yylo-benchmark 0.1.1-alpha.1'])(
    'refuses incompatible version %s before forwarding user arguments',
    async (reportedVersion) => {
      const { root, bin, record } = await fixture();
      await expect(invokeBenchmark(['run'], {
        cwd: root,
        env: {
          ...process.env,
          PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`,
          FAKE_RECORD: record,
          FAKE_BENCHMARK_VERSION: reportedVersion,
        },
      })).rejects.toMatchObject({ exitCode: 69 });
      await expect(readFile(record, 'utf8')).rejects.toMatchObject({ code: 'ENOENT' });
    },
  );

  it('bounds a stalled version handshake and reaps the child', async () => {
    const { root, bin, record } = await fixture();
    await expect(invokeBenchmark(['run'], {
      cwd: root,
      env: {
        ...process.env,
        PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`,
        FAKE_RECORD: record,
        FAKE_VERSION_WAIT: '1',
      },
      versionTimeoutMs: 50,
    })).rejects.toMatchObject({ exitCode: 69 });
    await expect(readFile(record, 'utf8')).rejects.toMatchObject({ code: 'ENOENT' });
  });
});
