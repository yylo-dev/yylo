# Simple workspaces

Simple mode is opt-in: one Git checkout holds code, notebooks, notes, agent
configuration, and Ledger. Existing managed metadata-only installations retain
their controller, task-worktree, and protected integration boundaries.

## Initialize explicitly

Use an ordinary Git repository, not a managed controller or linked worktree.
For a new folder, run `git init` yourself first; no-Git operation is not supported.
From the repository root, preview an external plan, inspect its paths and
preserved-file identities, then apply that exact plan:

```sh
yy init --mode simple --directory /absolute/project --plan-file /external/simple-plan.json
yy init --mode simple --apply-plan /external/simple-plan.json
```

Initialization writes only `.juno_task/config.json`, `.juno_task/.gitignore`,
`.juno_task/simple-agent-guidance.md`, and `.juno_task/simple-init.json`.
It does not install dependencies, create branches/worktrees, stage, or commit.
Ordinary dirty files are allowed. Existing root `AGENTS.md`, `CLAUDE.md`, and
`.gitignore` are preserved; conflicting managed instructions or ignore rules
that hide durable metadata refuse rather than being overwritten.

A changed plan preimage, existing metadata collision, symlink, registration
conflict, or interrupted initializer refuses. Preserve the reported files and
reservation; do not delete them or flip a mode field to force initialization.

## Work locally

Read `.juno_task/simple-agent-guidance.md` alongside your project instructions.
Run `yy pi` or supported generic agent dispatch in the root or a subdirectory.
The invocation directory remains the working directory; configuration and Ledger
remain bound to the validated Git root. A configured working-directory override
must resolve inside that same project, without symlink escapes or nested Git
projects. This routing check is not an operating-system sandbox for agent tools.

```sh
yy info --json
yy doctor workspace
yy ledger create "Investigate notebook convergence"
yy task local list
yy task local get ABC123
yy task local mark done ABC123 --response "Experiment recorded locally"
```

Replace `ABC123` with the returned task ID. `yy ledger` retains its ordinary
Record, history, revision, and concurrency behavior. Local `done` is bookkeeping,
not proof of tests, review, or protected-target delivery.

| Operation | Simple behavior |
| --- | --- |
| Agent dispatch | Edits the local checkout; respects explicit project configuration |
| Ledger and `task local list/get/mark` | Same-root task bookkeeping; no managed lifecycle receipts |
| `info`, workspace doctor, help | Mode/root and capability diagnostics; no provisioning |
| Managed `task start/run/finish`, leases, recovery | Refused; use local Ledger bookkeeping |
| `merge` and `integration` delivery | Refused; use separately authorized ordinary Git |
| Managed migration, bootstrap, topology changes | Not a Simple conversion mechanism |

Startup checks the installed compatible Ledger with bounded, non-installing
readiness probes. Missing or incompatible Ledger blocks dispatch with an explicit
recovery diagnostic. Select an environment, explicitly install the exact runtime
required by your CLI, and expose its executable on `PATH`; startup never performs
that installation for you. No project-local `install_requirements.sh` is needed.
An unrelated inherited controller root/role is a configuration error, never an
instruction to silently route your records into another project.

## Persistence and shared-checkout safety

Durable Records, history, and required content objects remain eligible for
explicit Git commits. Only cache, locks, runtime, logs, sessions, and secret paths
are ignored by the generated metadata-local ignore file. Do not blanket-ignore
`.juno_task`. Store credentials under `.juno_task/secrets`, not in tracked files
or task bodies. Initialization does not certify dependency or credential readiness.

Multiple agents share the same files and Git index: Ledger locking does **not**
isolate code edits. Coordinate overlapping edits and staging with the user.
Never reset, stash, overwrite, stage, commit, push, or clean implicitly. Explicit
ordinary Git commands remain available when authorized.

## Conversion is deferred

Neither split-to-Simple nor Simple-to-managed conversion is implemented by this
MVP. Do not modify a live controller's mode field. A future separately authorized
transition must inventory product history, Records, registrations, dirty/untracked
files, secrets, and collisions; prepare a fresh destination from the explicitly
selected product source; retain the old workspace and rollback identity. Stale
controller history is not a source for recovering product code.

## Package validation

`npm run test:simple` runs focused initialization, routing, session, and stubbed
agent-startup tests. `npm run test:simple-package` checks the built npm tarball in
a disposable real-Git project, using an explicitly selected compatible Ledger
via `YYLO_TEST_LEDGER_EXECUTABLE`. It requires a prior build and prepares fixture
runtime dependencies offline from the exact project lock with install scripts
disabled. Startup is checked for zero installer or model calls, and local
completion must produce no managed receipts.
