/** Canonical fresh Simple assets. No managed lifecycle or copied runtime scripts. */
export const SIMPLE_GUIDANCE = `# Simple workspace agent guidance

This Git checkout is both the project and Ledger root. Work on code, notebooks,
and notes locally; keep the invoking subdirectory as your working directory.
Read this file alongside existing project AGENTS.md and CLAUDE.md instructions.
Do not follow managed-controller guidance that requires a different worktree.
Use yy info --json or yy doctor workspace to inspect mode/root diagnostics.
Never convert an existing managed installation by changing its mode field.

Use yy task local or yy ledger for task bookkeeping. Done is not managed delivery.
Managed task start/finish, merge delivery and integration operations are unsupported.
Do not create branches/worktrees, stage, commit, push, reset, stash, or clean
anything implicitly. Git operations require explicit user authorization.
Preserve unrelated dirty files. Multiple agents share this checkout without
file isolation; coordinate overlapping edits and Git operations with the user.

Durable Ledger Records, history and content objects may be committed explicitly.
Runtime, cache, locks, logs and secrets are ignored, not durable project records.
Keep credentials under .juno_task/secrets, never in task bodies or tracked files.
Startup must use installed runtime capabilities, never a copied install hook or
an implicit package installation/upgrade. Missing dependencies require explicit
recovery in an explicitly selected environment with a compatible Ledger on PATH.
Initialization does not assert runtime readiness. No-Git execution and live
conversion between Simple and managed workspaces are outside this MVP.
`;

// Ledger 0.3.x stores canonical tasks, document/artifact revisions and objects
// outside these disposable directories. Never blanket-ignore .juno_task.
export const SIMPLE_IGNORE = `# Simple workspace: durable Records/history/objects remain Git-eligible.
/cache/
/locks/
/runtime/
/logs/
/sessions/
/secrets/
/.env*
`;

export const SIMPLE_CONFIG = {
  controllerWorkspace: { mode: 'simple', version: 1 },
  autoDependencyUpdate: false,
  skipHooks: true,
  envFilePath: '.juno_task/secrets/.env.yylo',
} as const;

export const SIMPLE_FILES: Readonly<Record<string, string>> = Object.freeze({
  '.gitignore': SIMPLE_IGNORE,
  'simple-agent-guidance.md': SIMPLE_GUIDANCE,
  'config.json': `${JSON.stringify(SIMPLE_CONFIG, null, 2)}\n`,
});
