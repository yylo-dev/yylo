# Juno 2.1 Metadata Controller

This folder is the default entry point for Juno agents. It owns Kanban, task
state, merge orchestration, and compact controller metadata; it does not contain
product code.

## Working contract

1. Use the controller-local Juno skills for Kanban, planning, project discovery,
   and explicitly requested Ralph execution.
2. Run Kanban and `yy task`/`yy merge` commands from this controller.
3. Read `.juno_task/config/task-workspace.json` for the exact product target and
   worktree root. For read-only product context, inspect the target ref or its
   registered integration-owner worktree.
4. Use `yy wiki` to discover controller and project guidance. Start implementation
   with `yy task start TASK_ID`, then change directory to the returned feature
   worktree and read its `AGENTS.md`/`CLAUDE.md` and project-specific skills.
   Task start runs the frozen project hydration workflow before reporting the
   worktree agent-ready. Before editing, inspect `$(yy wiki --path)/controller/task_dependency_hydration.md`
   and stop before implementation if hydration is missing, stale, or failed.
5. After a clean task commit, run `yy task finish TASK_ID` directly. Finish
   independently enforces admission and validation; repair closure defects while
   still `WORKING`. Optional read-only `yy task preflight TASK_ID` diagnoses
   admission before finish; it is not a prerequisite.
6. Tests and semantic reviews are explicit project checks outside merge. The
   native delivery adapter launches no models, chooses no reviewers, schedules
   no suites, and owns no repair or evidence-cache loop.
7. Observe one task with `yy merge status TASK_ID`. The target owner runs
   `yy merge land TASK_ID`, which automatically records the verified Git result in
   Ledger. Use `yy merge project TASK_ID` only to retry or repair that projection. Recompose after target movement; preserve private conflicts.
8. Integration always uses the ordinary task and merge lifecycle. The finite
   `yy migrate legacy-lifecycle` surface is restricted to existing umbrella
   inventory/drain/conversion; live apply requires separate owner authority.
   Package publication is maintainer-only, outside `yy`, and requires separate authority.
9. Never copy product code, bulky artifacts, or project-specific skill assets
   into this controller. Root instructions and core skills here are ignored local
   runtime files refreshed atomically from one bound immutable Juno package.

Expiry alone never grants ownership; use controller-proven successor or handoff
recovery and preserve dirty bytes. Controller checkpoints are best-effort warnings,
never lifecycle gates. Push, release, deploy, production mutation, and cleanup
require separate authority.
