# Simple and Advanced workspaces

Choose a mode for a **new project**. Normal initialization and CLI upgrades never
convert existing installations. The explicit `--from-advanced` plan/apply flow
below creates a separate Simple workspace without changing the source.

| | Simple (recommended for getting started) | Advanced |
| --- | --- | --- |
| Files | Code, notebooks, notes and Ledger in one Git checkout | Separate metadata controller and product worktrees |
| Tasks | Local Ledger bookkeeping | Managed task start/finish and merge delivery |
| Parallel agents | Shared files and Git index; coordinate edits | Isolated task worktrees |
| Git | Explicit owner-authorized ordinary Git | Managed protected-target delivery |

## One-command Simple initialization

```sh
git init                       # Explicit prerequisite in a new project folder
yy init --mode simple          # Initializes immediately, without confirmation
# Or select an existing independent Git root:
yy init --mode simple --directory /absolute/project
```

**Changed default side effects:** plain fresh Simple init now writes files rather
than printing a plan. Preview automation must select `--dry-run` (no writes) or
`--plan-file`. Initialization does not install dependencies or certify agent
readiness. Read `.juno_task/simple-agent-guidance.md`, then use `yy ledger` and
`yy pi` with separately installed dependencies and credentials.

## Interactive initialization

In the intended new project folder, run `git init` explicitly, then `yy init`.
The **last question** asks for Simple or Advanced, after directory, project goal,
agent, Git setup and existing-file confirmation. Simple is the recommended choice;
Advanced is the existing managed setup. Mode-specific files are created only after
selection. Simple requires an ordinary Git checkout and refuses existing managed
configuration; selecting Simple is not a way to override it.

The project goal is saved in Simple supplemental guidance and the chosen agent in
`defaultSubagent`. Simple does not clone a repository or configure a remote: leave
the Advanced-only Git URL blank. Root instructions are preserved. Dependencies
and credentials must already be installed/configured separately.

```sh
git init
yy init
# Skip only the mode question, retaining guided setup:
yy init --interactive --mode simple
# Existing inline automation stays Advanced; make it explicit if desired:
yy init "Build an API" --mode advanced
```

## Optional Simple previews and saved-plan automation

Use an ordinary Git repository, not a managed controller or linked worktree.
For a new folder, run `git init` yourself first; no-Git operation is not supported.
Independent Git repositories may live beneath another Simple or Advanced YYLO
workspace. Discovery stops at the child's Git boundary: the parent's metadata,
board, and runtime are not inherited. An uninitialized child must be initialized
locally; parent metadata is not a fallback. Multiple Simple roots inside the
same Git repository remain unsupported. Explicit managed registrations and
contradictory inherited environment assertions still receive safety checks.
For a read-only preview with no plan file, use `yy init --mode simple --dry-run`.
It reports the destination and proposed files, not successful initialization.
`--dry-run` cannot be combined with saved-plan, conversion, interactive or Advanced
options. Exact repeat initialization is a no-op; customized configuration is not
silently reconfigured.

For saved-plan automation, preview an external plan, inspect its paths and
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
| Managed migration, bootstrap, topology changes | Not a Simple conversion mechanism; use the explicit fresh-copy flow below |

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

## Convert Advanced to Simple: a fresh copy, not an in-place toggle

The supported direction is **Advanced → Simple**, narrowly scoped to an existing
registered **metadata-only controller**, with a same-repository product branch
agreed by its controller and task-workspace policies. Other legacy/combined
layouts, Simple → Advanced and manual mode-field toggles are unsupported, even
with `--force`. This is not a general migration engine.

Stop agents and other writers first. Every managed task must be `MERGED` or
`WITHDRAWN`, and every source worktree must be clean. Commit/preserve work explicitly;
the converter never finishes tasks, stages work, stashes, or deletes anything for you.
Ignored durable controller data, unknown states, symlinks, submodules, tracked
secret/runtime paths, nested agent configuration/managed instructions,
non-UTF-8 Git paths, sparse/assume-unchanged indexes, conflicting product-side Ledger data and existing
destinations are refused. Use a fresh directory with an existing parent, separate
from every source worktree and its Git storage. An unrelated ancestor YYLO
workspace does not prevent creating an independent destination repository.

Both supported lifecycle schemas (`juno_task_workspace_state.v1` and `.v2`)
are accepted after structure and settled-task validation. Pre-queue v1 may omit
`queues`; v2 requires the queue object and matching terminal tombstones.
Unknown schemas or malformed records refuse with a specific diagnostic; the
converter never upgrades or rewrites source lifecycle state.

```sh
# Read-only preview; store the plan outside source worktrees and Git storage.
yy init --mode simple --from-advanced /absolute/controller \
  --directory /absolute/new-simple-project --plan-file /external/conversion.json
# Review the paths and frozen identities in conversion.json, then explicitly apply:
yy init --mode simple --apply-plan /external/conversion.json
```

Without `--plan-file`, `--from-advanced` prints the same read-only preview. Apply
revalidates the complete plan before creating anything. Plans are bounded to 4 MiB;
individual Git reads/blobs to 16 MiB. Larger/unsupported projects need a separately
reviewed transition, not a force flag.

### What is preserved and what changes

- **Source unchanged:** controller, product refs/history, registrations and all
  existing worktrees remain intact. No automatic cutover or cleanup occurs.
- **New product checkout:** clones only the policy-selected product branch/history,
  never substitutes controller code. No source remote is retained, preventing an
  accidental push into the managed source. Select any future remote explicitly.
- **Durable data:** copies committed tasks, Ledger history, archives/receipts,
  document/artifact revisions, content objects, wiki, specs, workflows and tasks.md
  byte-for-byte. Old lifecycle state/receipts, cache, sessions, secrets and installed
  runtime remain at the source; they are not active Simple state. Task status is
  preserved as bookkeeping, not re-certified as managed delivery.
- **Instructions/configuration:** old product metadata, root AGENTS.md/CLAUDE.md,
  and root agent configuration folders are retained *inactive* under
  `.juno_task/advanced-backup`. The original root `.gitignore` is backed up before
  adding durable-metadata visibility rules. New active instructions use Simple
  guidance; the conversion receipt is `.juno_task/simple-conversion.json`.
- **No new commit:** destination changes are unstaged. Review them before explicitly
  committing. Already committed secrets remain in Git history: this command is not
  a credential scrubber. Ignored/untracked local secrets are never copied.

### Manual follow-up

1. Enter the new directory. Run `yy info --json` and `yy doctor workspace`.
2. Review inactive old instructions and bring over project-specific test commands
   and coding conventions. Do **not** restore Advanced task/worktree/merge rules.
   Preserved controller wiki/specs are historical knowledge, not Simple authority.
3. Configure desired agents/models and any compatible custom hooks deliberately;
   old managed configuration is not activated automatically. Install dependencies
   and restore credentials through your normal secret-safe process.
4. Choose the new folder as your working project. The two Ledgers are independent
   snapshots, not synchronized boards. Keep the original as the rollback source.

If apply is interrupted, preserve the new destination and its `.yylo-simple-init`
reservation for inspection; it is not agent-ready. Do not remove the reservation
to force startup. The source still works unchanged. After resolving the cause,
prepare a new plan for another fresh directory; retries never overwrite a prior
attempt. No automatic rollback/cleanup framework is involved.

## Package validation

`npm run test:simple` runs focused initialization, routing, session, and stubbed
agent-startup tests. `npm run test:simple-package` checks the built npm tarball in
a disposable real-Git project, using an explicitly selected compatible Ledger
via `YYLO_TEST_LEDGER_EXECUTABLE`. It requires a prior build and prepares fixture
runtime dependencies offline from the exact project lock with install scripts
disabled. Startup is checked for zero installer or model calls, and local
completion must produce no managed receipts.
