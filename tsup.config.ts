import { defineConfig } from 'tsup';

export default defineConfig({
  entry: {
    index: 'src/index.ts',
    'utils/release-bundle': 'src/utils/release-bundle.ts',
    'bin/cli': 'src/bin/cli.ts',
    'bin/invocation-boundary': 'src/bin/invocation-boundary.ts',
    'bin/feedback-collector': 'src/bin/feedback-collector.ts'
  },
  format: ['esm', 'cjs'],
  target: 'node18',
  platform: 'node',
  tsconfig: 'tsconfig.build.json',

  // Code splitting and bundling
  splitting: false,
  bundle: true,
  minify: false,

  // Source maps and debugging
  sourcemap: true,
  clean: true,

  // TypeScript declarations
  dts: true,

  // External dependencies (don't bundle)
  external: [
    'fsevents'  // macOS-specific, optional dependency
  ],

  // Environment and Node.js specific
  shims: false,

  // Output configuration
  outDir: 'dist',

  // Banner for CLI executable
  banner: {
    js: '#!/usr/bin/env node'
  },

  // ESBuild options
  esbuildOptions: (options) => {
    options.conditions = ['node'];
    options.mainFields = ['module', 'main'];
    // Suppress unused import warnings for bundled dependencies
    options.ignoreAnnotations = false;
    options.treeShaking = true;
    // Suppress warnings about unused imports from bundled modules
    options.logLevel = 'warning';
    options.logLimit = 0;
  },

  // Only add shebang to CLI entries
  onSuccess: 'chmod +x dist/bin/cli.js && chmod +x dist/bin/feedback-collector.js',

  // Development mode
  watch: process.env.NODE_ENV === 'development',

  // Enable tree shaking
  treeshake: true,

  // Target specific output
  outExtension({ format }) {
    return {
      js: format === 'esm' ? '.mjs' : '.js'
    };
  },

  // Define globals for better optimization
  define: {
    __VERSION__: JSON.stringify(process.env.npm_package_version || '2.0.1'),
    __DEV__: process.env.NODE_ENV === 'development' ? 'true' : 'false'
  },

  // Inject package.json version
  inject: ['./src/version.ts'],

  // Ensure proper module format
  cjsInterop: true,
  legacyOutput: false
});