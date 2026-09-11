# Watching managed progress

Use the first-class watch surface for commands that may outlive one ordinary
shell tool call. It owns the child process group, bounded combined log, private
run directory, terminal metadata, and the strict `juno.watch-footer.v1` footer.
Do not assemble a producer with heredocs and do not use `sleep; tail` polling.

## Decision rule

```text
new command you own       -> yy watch exec -- COMMAND...
already detached watch run -> yy watch status RUN_ID / yy watch follow RUN_ID / yy watch await RUN_ID
coherent task checkpoint   -> yy task checkpoint TASK_ID; yy evidence run TASK_ID
waiting for task evidence  -> yy evidence await TASK_ID
external one-shot blocker  -> await_blocker.py --then ...
```

These commands grant no implementation, review, release, push, deployment, or
production authority. They only execute an already-authorized argv.

## Foreground command

```bash
yy watch exec --timeout 900 -- npm test -- src/cli/__tests__/main.test.ts
```

Foreground mode returns the command's canonical exit code and prints the
terminal `juno.watch-run.v1` record. Combined output is retained in the run's
private log rather than mixed with the machine record.

## Detached command

```bash
start=$(yy watch exec --detach --timeout 900 -- npm test)
run_id=$(printf '%s\n' "$start" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
yy watch status "$run_id"
yy watch await "$run_id"
```

`status` is read-only. `await` observes the bound producer and returns its exit
code. Timeout or interruption sends TERM and then bounded KILL only to the owned
process group. Unrelated process groups are never cleanup targets.

## Read-only log follower

`follow` reads `combined.log` from byte zero, follows appended bytes, and returns
the producer exit code only after observing the exact atomic footer. It never
signals or acquires ownership of the producer. Ctrl-C exits only the follower.
Malformed or missing footers are not terminal truth.

```bash
# Direct terminal or tmux pane: semantic ANSI when stdout is a TTY
yy watch follow "$run_id"
tmux split-window -h "yy watch follow '$run_id'"

# Stable plain semantic layout
NO_COLOR=1 yy watch follow "$run_id"
yy watch follow "$run_id" | cat

# Raw log access remains available without presentation
tail -F ".juno_task/runtime/watch-runs/$run_id/combined.log"
cat ".juno_task/runtime/watch-runs/$run_id/combined.log"
```

The follower colors only exact `[THINKING]`, `[TOOL]`, `[INPUT]`,
`[TOOL_RESPONSE]`, `[ANSWER]`, and `[STATUS]` grammar. A tool response is red
only when its enclosing compact tool metadata contains structured
`"isError":true`; arbitrary words such as `error`, `failed`, or `blocked` do
not select error styling. Unknown tags, malformed metadata, and non-Pi logs pass
through unchanged. `NO_COLOR` and pipes preserve the same text and spacing.

## Task validation evidence

A task is the unit of intent and may contain several commits. A commit is not
automatically a validation request.

```text
WIP commit                  -> no automatic validation
coherent committed tip      -> yy task checkpoint TASK_ID
run selected local evidence -> yy evidence run TASK_ID
read/await evidence         -> yy evidence status|await TASK_ID
final clean tip             -> yy task finish TASK_ID
```

Checkpoint planning selects registered focused validation and binds task, base,
tip, tree, changed paths, command, dependency locks, controller policy, runtime,
and local runner class. Unknown or mixed ownership falls back conservatively.
A later tip reuses a command only when its complete input closure remains exact.
`yy task finish` creates the final checkpoint, reuses valid receipts, runs only
missing commands, and binds the receipts into the immutable task closure. Tests
and semantic review end here; native delivery does not rerun or reinterpret them.

## Terminal files

Runs live under the canonical controller's private
`.juno_task/runtime/watch-runs/RUN_ID/` directory:

```text
run.json       juno.watch-run.v1 state and process identity
pid            owned child/process-group ID
combined.log   bounded observation source
footer         strict atomic terminal footer
```

The footer remains exact ASCII:

```text
schema_version=juno.watch-footer.v1
exit_code=0
completed_utc=2026-08-12T21:09:28Z
```

A valid footer is terminal producer truth; it does not convert a nonzero command
into success. Empty, partial, reordered, duplicate, unknown-field, invalid-time,
and out-of-range bytes fail closed. `run.json` additionally records timeout and
signal truth. Run directories and standing-evidence receipts are private runtime
state; deletion is a separate cleanup action.

## Legacy attachment

`watch_progress.py --pid-file ... --log-file ... --footer-file ...` remains the
strict observer for a pre-existing producer. It never signals that producer.
New producers should use `yy watch exec` so PID publication, logging, footer
publication, timeout handling, and descendant settlement are not hand-written.

## Managed task execution and native delivery

Use `yy task run TASK_ID` for the controller-owned typed implementation path.
After it queues the immutable source, observe `yy merge status TASK_ID`, run
`yy merge land TASK_ID`, then separately run `yy merge project TASK_ID`. Merge
has no managed driver, FIFO scope, lifecycle YAML, model prompt, review, repair,
or validation scheduler. It preserves a private conflict and refuses stale
target updates rather than inheriting release, push, deploy, or other external
authority.

Task lifecycle YAML and prompts are controller-owned committed assets. A task
run freezes the controller commit, template and prompt digests, compiler,
runtime, model, and budget identities. Customized assets are preserved by
ordinary managed updates, active attempts are immutable, and automatic
model-authored template or prompt mutation is refused.

Command decisions report `executed`, `reused`, `invalidated`, `skipped`, or
`not_applicable`. Inert configured text has an exact zero-command proof; active
product documentation runs its cheap audit. Grouped coherence and parsed test
result integrity run before suites/review. High-risk work overlaps Reviewer A
with the suite but still launches B only after A PASS; blocking A cancellation
is receipt-backed.
