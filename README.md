# YYLO CLI

YYLO is a command-line orchestrator for coding agents, repeatable workflows, and receipt-backed repository changes. It is for developers who want a quick agent loop and for project operators who need typed task, validation, merge, and release-readiness boundaries.

- npm package: [`@yylo/cli`](https://www.npmjs.com/package/%40yylo%2Fcli)
- Commands: `yylo` and `yy` (equivalent); `ypl` is `yy pi --live`
- Source: [yylo-dev/yylo](https://github.com/yylo-dev/yylo)

YYLO orchestrates work. [YYLO Ledger](https://github.com/yylo-dev/yylo-ledger) is the independent Git-native Record/task store, and [YYLO Benchmark](https://github.com/yylo-dev/yylo-benchmark) is the independent evaluation/evidence package. `yy ledger` and `yy benchmark` delegate to those separately installed CLIs; they are not bundled alternate implementations.

## Quick start: install to first successful command

**Prerequisites:** Node.js 20.10 or newer, npm, and Git.

```bash
npm install --global '@yylo/cli@latest'
yy --version

mkdir yylo-demo
cd yylo-demo
git init
yy init --task "Document the onboarding path" --subagent pi
yy watch exec pwd
```

A successful run prints the installed YYLO version, initializes `.juno_task/`, then emits a watch receipt with `"state":"COMPLETED"`, `"exit_code":0`, and nonzero `log_bytes`. This canary does not contact a model provider.

Inspect the initialized workspace and exact command surface:

```bash
yy info --json
yy doctor workspace
yy --help
```

`doctor workspace` is intentionally nonzero when it finds an actionable topology problem; it never fetches or changes the workspace.

### Stable and prerelease channels

The stable npm channel is `@latest` (`0.2.0` when this README was audited). The current prerelease is on `@next` (`0.2.1-rc.14`), not `latest`:

```bash
# Stable
npm install --global '@yylo/cli@latest'

# Explicit prerelease
npm install --global '@yylo/cli@next'
# Exact prerelease for reproducible installs
npm install -g @yylo/cli@0.2.1-rc.14

npm view '@yylo/cli' version dist-tags --json
yy --version
```

Pin an exact version in CI. Installing `@next` is an intentional prerelease choice.
The first guarded release-helper checkpoint is exact `--set v0.1.0-rc.1`; later releases must use their separately authorized exact SemVer.

Next: [run an agent](#beginner-agent-workflow), [manage a typed task](#typed-task-and-merge-flow), or [build a managed workflow](#managed-workflows-and-evidence).

## What YYLO owns

| Need | Public surface | Boundary |
| --- | --- | --- |
| Agent run | `yy pi`, `yy start`, and the agent aliases listed by `yy --help` | Provider credentials and model availability remain external. |
| Session continuity | `continue`, `clone`, `branches`, `switch`, `continuity` | Scope state is isolated and explicit; cleanup is planned and reversible. |
| Observable commands | `watch exec|status|await` | Bounded logs and terminal machine truth; no hidden background ownership. |
| Validation evidence | `evidence run|status|await` | Content-addressed task evidence tied to exact inputs. |
| Repository topology | `info`, `where`, `doctor workspace`, `integration` | Read-only discovery is separate from guarded sync/repair/push. |
| Feature lifecycle | `task start|run|status|checkpoint|preflight|finish` | Implementation belongs in the returned exact-base task worktree. |
| Protected delivery | `merge status|plan|arbiter|drive|next|resolve` | One fenced target owner and expected-old-SHA CAS; dirt is preserved. |
| Release epoch | `release train ...` | Readiness only; tag, publish, push, deploy, and cleanup need separate authority. |
| Records/evaluation | `ledger`, `benchmark` | Transparent delegation to independently installed canonical packages. |

Run `yy --help` for the complete top-level inventory of your installed version; each listed command prints its own usage when invoked with `-h`. The old `lifecycle` command is removed; use typed `task` and `merge` commands.

## Beginner agent workflow

Install the coding agent you intend to use and configure its provider credentials separately. Pi is optional and supports multiple providers:

```bash
npm install --global '@mariozechner/pi-coding-agent'
yy pi --help
yy pi --no-session 'Summarize this repository and make no changes'
```

The final command may contact the configured model provider. `--no-session` prevents Pi session persistence; it does not disable provider usage.

For a reusable prompt or shell-sensitive text, prefer a file:

```bash
printf '%s\n' 'Explain the test layout. Do not edit files.' > prompt.md
yy pi --prompt-file prompt.md --no-session
```

Interactive Pi uses the `ypl` shortcut:

```bash
ypl 'Inspect the current task'
```

`ypl` expands to `yy pi --live`. Live mode requires an interactive terminal and enabled Pi extensions.

### Controlled iterations

```bash
yy -s pi -m :gpt -i 3 -p 'Implement the next small verified increment'

yy loop -n 2 \
  --step 'yy pi "Implement the next increment"' \
  --step 'npm test'
```

Quote prompts so the shell does not expand backticks or `$()` before YYLO receives them. `-i` bounds iterations inside an agent invocation; `yy loop -n` bounds the outer command workflow.

The loop's `-n/--iterations` bounds the outer command workflow; agent
`-i/--max-iterations` still bounds iterations inside an agent invocation.
Inline steps are repeatable:

```bash
yylo loop -n 5 \
  --step 'yy pi "Implement the next increment"' \
  --step 'yy cc "Inspect and improve your work"' \
  --step 'npm test'
```

For a reusable workflow, save the same contract as `flow.yaml`:

```yaml
iterations: 5
continuity: iteration
on_error: continue
steps:
  - run: yy pi "Implement the next increment"
  - run: yy cc "Inspect and improve your work"
  - run: npm test
```

```bash
yylo loop --workflow flow.yaml
```

Every step receives one-based loop metadata through `YYLO_LOOP_ID`,
`YYLO_ITERATION`, `YYLO_ITERATION_COUNT`, `YYLO_STEP`, and `YYLO_STEP_COUNT`.

## Models and project shortcuts

`yy pi --help` is the source of truth for shipped aliases. Current Pi shortcuts include:

| Shortcut | Resolved model |
| --- | --- |
| `:luna` | `openai-codex/gpt-5.6-luna` |
| `:sol` | `openai-codex/gpt-5.6-sol` |
| `:gpt` | `:sol` (Pi default) |
| `:mini` | `openai-codex/gpt-5.6-terra` |
| `:sonnet` | `anthropic/claude-sonnet-4-6` |
| `:opus` | `anthropic/claude-opus-4-6` |

Aliases are subagent-specific; for example, Pi and Codex do not share the same `:mini` mapping.

Set a per-project default:

```bash
yy pi set-default-model :sol
```

Add project shortcuts in `.juno_task/config.json`:

```json
{
  "modelShortcuts": {
    "pi": {
      ":team-default": ":sol"
    }
  }
}
```

Project shortcuts are scoped to the selected subagent and can reference shipped or project shortcuts. Unknown targets, malformed data, and cycles fail with an actionable error. Managed Workflow Runner model authorization is separate: explicit selectors must be exact members of `workflowModels`; an unflagged `yy pi` continues to inherit the configured default.

## Observable local commands

`watch` owns bounded execution evidence for an ordinary local command:

```bash
yy watch exec npm test
# Use the run ID printed above:
yy watch status RUN_ID
yy watch await RUN_ID
```

`RUN_ID` is a placeholder. Status is observation; it does not acquire task, merge, or release authority. Watch evidence includes terminal state and bounded logs rather than requiring terminal-scrollback reconstruction.

## Managed workflows and evidence

Fresh `yy init` installs managed scripts, prompts, and wiki guidance under `.juno_task/`. Update checksum-managed assets with:

```bash
yy scripts update
```

Locally customized files are preserved and a package candidate is written to a managed-conflict path. `--force` is an explicit replacement operation and creates backups; inspect conflicts before using it.

### Workflow Runner

Use a reviewed YAML workflow for ordered, repeatable steps:

```bash
./.juno_task/scripts/workflow_runner.sh \
  --init-example agent-chain .juno_task/workflows/agent-chain.yaml
./.juno_task/scripts/workflow_runner.sh lint \
  --workflow .juno_task/workflows/agent-chain.yaml
./.juno_task/scripts/workflow_runner.sh \
  --workflow .juno_task/workflows/agent-chain.yaml --dry-run \
  --print-output none --no-print-step-stdout
```

A workflow run retains rendered command identity, stdout/stderr, responses, session IDs, declared receipt hashes, attempts, and terminal manifests. `--tmux` creates an observer session; it does not detach the producer.

If a producer was interrupted, diagnose before mutation:

```bash
./.juno_task/scripts/workflow_runner.sh recover-attempt RUN_DIRECTORY --dry-run
./.juno_task/scripts/workflow_runner.sh doctor RUN_DIRECTORY
```

`RUN_DIRECTORY` is the printed durable run path. Recovery resumes only the first invalid step after verifying unchanged successful evidence. Never edit historical manifests to make them reusable.

### Task validation evidence

At a clean coherent task commit:

```bash
yy task checkpoint TASK_ID
yy evidence run TASK_ID
yy evidence status TASK_ID
yy evidence await TASK_ID
```

These commands plan and retain exact-input validation evidence. They do not finish or merge the task.

## Typed task and merge flow

This section is for repositories initialized with the current controller/task policy. Run lifecycle commands from the registered metadata controller. Never edit the integration-owner checkout.

### Managed path

```bash
yy task run TASK_ID
yy merge drive --through TASK_ID
```

`TASK_ID` is a Ledger task ID. `task run` executes the controller-owned workflow through `QUEUED`; `merge drive` is an explicit fenced target mutation.

### Manual implementation path

```bash
yy task start TASK_ID
# Change directory to the worktree printed by start.
# Read its AGENTS.md/CLAUDE.md, implement, run focused tests, and commit.
yy task preflight TASK_ID
yy task finish TASK_ID
yy merge status
yy merge arbiter status
yy merge arbiter run --through TASK_ID
```

The guarded manual admission order is `yy task preflight ID -> yy task finish ID`; merge remains a separate queue-owned step.

Safety invariants:

1. `task start` freezes the protected target SHA, creates a dedicated branch/worktree, and completes configured dependency hydration before reporting `WORKING`.
2. Product edits and focused tests occur only in that task worktree. Controller metadata and integration-owner product bytes are separate authorities.
3. `preflight` is read-only and catches closure defects before expensive gates. `finish` requires a clean committed tip and queues it; it does not merge.
4. The merge queue owns risk-based review and moved-target composition. Low risk has no semantic reviewer, normal risk at most one, and high risk two sequential reviewers on one frozen candidate. After one repair candidate and one delta-review group, unresolved findings stop as `REVIEW_FINDINGS_EXHAUSTED` instead of starting an unbounded review loop.
5. Target mutation is serialized under one fencing owner and expected-old-SHA CAS. Lease age alone never transfers authority.
6. Conflicts and unrelated dirty bytes are preserved. Use the exact recovery packet and `yy merge resolve TASK_ID`; do not reset, stash, force, rebase, or squash to bypass it.

Observation commands are safe to repeat:

```bash
yy task status TASK_ID
yy task doctor TASK_ID
yy merge plan TASK_ID --json
yy merge status
yy merge arbiter status
```

`yy merge next` and `yy merge resolve` are explicit recovery mutations, not polling commands.

## Sealed release epochs

A release wave can freeze all eligible pre-cutoff candidates, compose one private history-preserving train, run aggregate evidence once, and advance the protected target with one expected-old-SHA CAS.

```bash
yy release train inspect /absolute/path/to/train.json --json
yy release train seal /absolute/path/to/train.json --json
# Retain the epoch ID and one-time token returned by seal.
yy release train drive EPOCH_ID --epoch-token TOKEN --json
yy release train epoch-status EPOCH_ID --json
```

The declaration path, `EPOCH_ID`, and `TOKEN` are placeholders. `inspect`, `plan`, `status`, `epoch-status`, and `shadow` are observations. `seal`, `drive`, `eject`, `repair`, and `retry` are fenced mutations with command-specific authority.

A successful epoch emits read-only release readiness after target CAS and member reconciliation. It does **not** authorize an RC, tag, push, npm/PyPI publication, deployment, production mutation, or worktree cleanup. Those remain separate explicit actions.

## Workspace roles and recovery

| Workspace | Use it for | Do not use it for |
| --- | --- | --- |
| Metadata controller | Ledger/task/merge/release orchestration and durable receipts | Product implementation or target integration edits |
| Task worktree | Scoped implementation, focused tests, coherent commits | Private controller state or protected-target mutation |
| Integration owner | Clean latest integrated reads and guarded target ownership | Feature edits, Kanban/session writes, or dirt cleanup |

Discover routing without changing it:

```bash
yy info --json
yy where controller
yy where integration
yy where target
yy where task TASK_ID
yy doctor workspace
```

Routing is registration-based and fail-closed; YYLO does not guess a nearby controller, switch branches, or clean a checkout to manufacture compliance.

For integration-owner inspection and guarded refresh:

```bash
yy integration status
yy integration sync
```

`integration status` observes. `integration sync` is a mutation: it refuses dirty/diverged/ambiguous state and verifies nested gitlink availability before moving anything. `yy integration push` is separate remote authority and must not be inferred from sync, merge, or release readiness.

## Ledger and Benchmark delegates

Install canonical packages independently:

```bash
python3 -m pip install 'yylo-ledger==0.2.0'
npm install --global '@yylo/benchmark@0.1.0-rc.1'

yylo-ledger --help
yy ledger --help
yylo-benchmark --help
yy benchmark --help
```

Delegation preserves arguments, stdin/stdout/stderr, cwd, exit status, and signals. It never silently chooses a checkout-local or legacy executable. See the [Ledger repository](https://github.com/yylo-dev/yylo-ledger) and [Benchmark repository](https://github.com/yylo-dev/yylo-benchmark) for package-specific guidance and prerelease boundaries.

## Completion and help

```bash
yy completion install
yy completion status
yy help
yy --help
```

Completion supports Bash, Zsh, and Fish. Use the nested help for your installed release rather than copying an option from a different channel.

## Source-checkout toolchain (advanced)

A monorepo checkout containing `juno-code/` and `juno_kanban/` can build isolated source aliases without replacing normal global `yy`:

```bash
./juno-code/scripts/juno-002-source-toolchain.sh install
export PATH="$PWD/.juno_toolchain/juno-002/bin:$PATH"
yy-juno-002 --version
juno-kanban-juno-002 --version
./juno-code/scripts/juno-002-source-toolchain.sh status
```

Both aliases enforce the exact Ledger compatibility policy `>=2.0.5,<3.0.0`. Source selection, controller registration, and data history are separate boundaries:

```bash
./juno-code/scripts/juno-002-source-toolchain.sh register-controller /path/to/controller controller-branch
./juno-code/scripts/juno-002-source-toolchain.sh controller-status
./juno-code/scripts/juno-002-source-toolchain.sh rollback-selection
```

The path and branch are placeholders. `rollback-selection` changes only isolated executable selection. Switching branches never downgrades or restores Ledger data. Back up and migrate a board through Ledger's reviewed data procedures.

| Source lane | Owns | Must not do |
| --- | --- | --- |
| Controller | Metadata, orchestration, prompts, and receipts | Product implementation or implicit ref changes |
| Task checkout | Scoped implementation and tests | Protected-target mutation |
| Integration owner | Guarded candidate integration | Kanban/session writes or unrelated edits |
| Small fix worktree | Exact-base small product repair | Bypass task/review/candidate boundaries |

## Development

```bash
git clone https://github.com/yylo-dev/yylo.git
cd yylo
npm ci
npm test
npm run typecheck
npm run build
node dist/bin/cli.mjs --help
```

The monorepo may embed YYLO CLI alongside Benchmark and a Ledger submodule, but each public package has its own manifest, version, release contract, and registry channel. A source checkout version is not evidence that the same version was published.

## Help and links

- npm: [@yylo/cli](https://www.npmjs.com/package/%40yylo%2Fcli)
- Source/issues: [yylo-dev/yylo](https://github.com/yylo-dev/yylo)
- Ledger: [yylo-dev/yylo-ledger](https://github.com/yylo-dev/yylo-ledger)
- Ledger package: [yylo-ledger on PyPI](https://pypi.org/project/yylo-ledger/)
- Benchmark: [yylo-dev/yylo-benchmark](https://github.com/yylo-dev/yylo-benchmark)

## License

MIT.
