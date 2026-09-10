---
juno_prompt_schema: juno.life_cycle.v1
public_macro: "@@life_cycle"
revision: 6
---

# Observable Juno task lifecycle

Treat all caller text after this prompt as the requested ordered task set, goal,
or release boundary. Preserve that payload literally and exactly once. In
particular, do not reinterpret or remove multiline task IDs, `##TASK_ID`,
`@@no_code`, quotes, backticks, `$ARGUMENTS`, `$1`, `$2`, `$@`, or shell syntax.

Use the installed Juno control plane; do not create another workflow engine.

1. **Discover before mutation.** Run `yy info --json`, `yy doctor workspace`, and
   the canonical Kanban reads. Record controller, target ref/SHA, registered
   integration owner, installed/controller/runtime versions, Node version, task
   ownership, blockers, and safe parallel opportunities. Bind the project-
   supported Node runtime before `yy` or managed Pi execution. Never assume the
   invocation directory is the product checkout.
2. **Keep Kanban and receipts authoritative.** Deduplicate every newly proven
   Juno defect before filing it with evidence. Do not absorb unrelated work.
   Start implementation only with `yy task start TASK_ID`; use the exact returned
   worktree and immutable receipt/path scope. Do not impersonate lifecycle state
   or edit receipts.
3. **Hydrate locally.** In each admitted task worktree use exact-lock dependency
   installation (`npm ci` where applicable). Never symlink dependencies. Keep
   controller metadata and product bytes on their declared surfaces.
4. **Make execution observable.** Own new long-running commands with bounded
   `yy watch exec -- COMMAND...`; use `--detach` only when necessary and then
   observe the returned run ID with `yy watch status|await`. Use `yy evidence
   await TASK_ID` for standing task evidence. Never construct PID/log/footer
   plumbing, use `sleep; tail`, or make a model poll. The managed watcher owns
   the process group and strict terminal truth described by
   `$(yy wiki --path)/watching_progress.md`.
5. **Implement narrowly.** Give the agent bounded task requirements, exclusions,
   exact paths, and proportional focused tests. Commit one logical task at a
   time. Before shared heavy real-Git suites, inspect active workloads and avoid
   intentional resource-lock contention.
6. **Keep semantic review queue-owned and bounded.** Implementation and repair
   agents never launch lifecycle-semantic reviewers. The managed merge queue is the sole lifecycle-semantic review owner:
   low risk uses zero reviewers, normal at most one, and high uses Reviewer A then Reviewer B
   sequentially against one frozen predecessor-bound v1 candidate. It permits at most one repair candidate
   and one delta review group; further material findings stop as `REVIEW_FINDINGS_EXHAUSTED`,
   never an autonomous review loop.
7. **Use managed lifecycle boundaries.** After a clean committed implementation,
   run read-only `yy task preflight TASK_ID`, repair closure defects while
   `WORKING`, then run separately authorized `yy task finish TASK_ID`. Observe
   delivery with `yy merge status|arbiter status`; one fenced target owner uses
   `yy merge arbiter run` or typed `yy merge drive` for expected-old-SHA delivery,
   while `next|resolve` remain explicit recovery mutations. Never steal on elapsed time or discard dirty
   recovery bytes. For integration-owner drift use `yy integration status`, then
   receipt-bound `repair --dry-run/--apply`.
8. **Keep release authority explicit.** Integrate every change through the ordinary
   task and merge lifecycle. Package preparation uses the repository maintainer
   release script only after the version-bump task is integrated. RC/tag creation,
   push, publication, deployment, production mutation, cleanup, and post-deploy
   E2E remain distinct authorities outside `yy`.
9. **Hand off truth.** Report ordered task outcomes, exact commits and SHAs,
   sessions, costs where available, durations, tests, PID/log/footer paths,
   Kanban updates/new bugs, blockers, contention waits, canary limitations, and
   remaining safe parallel opportunities. Leave every unauthorized action undone.

## Evolution

This is schema `juno.life_cycle.v1`, revision 7. Change the canonical source in
`juno-code/src/templates/prompts/life_cycle.md`, update this revision and release
notes when behavior changes, and validate source/dist/tarball plus managed-install
parity. Project customizations are user-owned: managed update must preserve or
surface conflicts rather than silently replacing customized bytes.
