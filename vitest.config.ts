import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

// Keep the suites that share the cross-process real-Git install lease in one
// file lane. Independent npm processes still contend on the diagnostic lease.
export const MANAGED_INSTALL_POOL_MATCH_GLOBS: [string, 'forks'][] = [
  ['src/cli/__tests__/benchmark-command.test.ts', 'forks'],
  ['src/utils/__tests__/managed-controller-recovery.test.ts', 'forks'],
  ['src/utils/__tests__/managed-project-assets.test.ts', 'forks'],
  ['src/utils/__tests__/script-installer.test.ts', 'forks'],
  ['src/utils/__tests__/task-workspace-fixture-modes.test.ts', 'forks'],
  ['src/utils/__tests__/task-workspace.test.ts', 'forks'],
];

/**
 * Wave 1 (7djT8N) retry policy: ordinary failures execute exactly once.
 * Retries are a reported quarantine affordance only — an explicit opt-in
 * through YYLO_TEST_QUARANTINE_RETRIES that admission argv never sets, so a
 * retried pass can never become an eligible first-pass receipt.
 */
export function quarantineRetryCount(
  environment: NodeJS.ProcessEnv = process.env,
): number {
  const raw = environment.YYLO_TEST_QUARANTINE_RETRIES?.trim();
  if (!raw) return 0;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0 || value > 5) {
    throw new Error(
      `YYLO_TEST_QUARANTINE_RETRIES must be an integer in [0, 5]; received ${JSON.stringify(raw)}`,
    );
  }
  if (value > 0) {
    // Explicit, reported quarantine: the structured marker is emitted to the
    // console and captured in raw logs; lifecycle admission argv carries no
    // such environment so its receipts stay first-pass-only.
    process.stderr.write(
      `[quarantine] retries=${value} reason=YYLO_TEST_QUARANTINE_RETRIES results=advisory-not-first-pass\n`,
    );
  }
  return value;
}

export default defineConfig({
  test: {
    globals: true,
    // Wave 1 (7djT8N): Node is the default environment; the suite has no
    // browser-DOM dependence (verified by the vitest-policy tests). Files
    // that later need a DOM must opt in with a `@vitest-environment happy-dom`
    // docblock so Node-only runs never load happy-dom.
    environment: 'node',
    globalSetup: ['./src/test-utils/global-setup.ts'],
    setupFiles: ['./src/test-utils/setup.ts'],
    include: [
      'src/**/*.{test,spec}.{js,ts,tsx}',
      'src/**/__tests__/**/*.{js,ts,tsx}'
    ],
    exclude: [
      'node_modules',
      'dist',
      'coverage',
      'src/test-utils/**',
      'src/**/__tests__/helpers/**',
      '**/*.d.ts',
      'src/cli/__tests__/utils.test.ts', // broken: references nonexistent modules (completion.js, test-runner.js)
    ],

    // Coverage configuration
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.{js,ts,tsx}'],
      exclude: [
        'src/**/*.{test,spec}.{js,ts,tsx}',
        'src/__tests__/**',
        'src/test-utils/**',
        'src/**/*.d.ts',
        'src/**/types.ts',
        'src/**/constants.ts',
        'src/index.ts',
        'src/bin/**',
        'src/version.ts'
      ],

      // Coverage thresholds — current baseline; raise as coverage improves
      thresholds: {
        global: {
          branches: 40,
          functions: 25,
          lines: 10,
          statements: 10
        }
      },

      // Coverage watermarks for display
      watermarks: {
        statements: [80, 95],
        functions: [80, 95],
        branches: [80, 95],
        lines: [80, 95]
      },

      // Report uncovered lines
      reportOnFailure: true,
      skipFull: false
    },

    // Test execution
    testTimeout: 10000,
    hookTimeout: 10000,
    retry: quarantineRetryCount(),
    bail: 1,  // Stop on first failure in CI

    // Performance
    pool: 'threads',
    poolMatchGlobs: MANAGED_INSTALL_POOL_MATCH_GLOBS,
    poolOptions: {
      threads: {
        singleThread: false,
        isolate: true,
        useAtomics: true
      },
      forks: {
        // Only the two managed-install suites use this serial lane; ordinary
        // unit files remain in the concurrent thread pool.
        singleFork: true,
        isolate: true
      }
    },

    // Reporters
    reporter: process.env.CI
      ? ['verbose', 'github-actions', 'json']
      : ['verbose'],

    // Output
    outputFile: {
      json: './test-results/results.json',
      html: './test-results/results.html'
    },

    // Mock handling
    clearMocks: true,
    restoreMocks: true,
    mockReset: true,

    // Test types - disabled for now
    typecheck: {
      enabled: false,
      only: false,
      checker: 'tsc'
    },

    // Watch mode
    watch: !process.env.CI,
    watchExclude: [
      'node_modules/**',
      'dist/**',
      'coverage/**',
      'test-results/**'
    ],

    // Environment variables
    env: {
      NODE_ENV: 'test',
      VITEST: 'true'
    }
  },

  // Path resolution
  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
      '@/cli': resolve(__dirname, './src/cli'),
      '@/core': resolve(__dirname, './src/core'),
      '@/templates': resolve(__dirname, './src/templates'),
      '@/utils': resolve(__dirname, './src/utils'),
      '@/types': resolve(__dirname, './src/types'),
      '@/test-utils': resolve(__dirname, './src/test-utils')
    }
  },

  // ESBuild options for test compilation
  esbuild: {
    target: 'node18',
    format: 'esm'
  },

  // Define globals
  define: {
    __VERSION__: JSON.stringify('test'),
    __DEV__: 'true'
  }
});
