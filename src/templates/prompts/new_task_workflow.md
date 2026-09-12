# Start a feature task

Use the canonical controller to select one Kanban task, then run:

`TASK_ROOT` names that canonical controller. Control-plane routing never switches or cleans the checkout where the user invoked `yy`.

```text
yy task start TASK_ID
```

The project-owned task-workspace policy supplies the full product target ref, allowed product paths, focused validation, branch prefix, and worktree root. Start freezes the exact current target SHA, creates one task branch and one product worktree, and writes a compact controller record. It is idempotent only while that worktree is still clean at the exact frozen base; any branch, path, target, or record drift refuses.

Implement and commit only inside the returned product worktree. Immediately after start and before editing or testing, follow the exact-lock, validation-cwd-aware hydration contract in [task dependency hydration](../wiki/controller/task_dependency_hydration.md). Stop before implementation if provisioning or its clean-tree check fails. The controller keeps Kanban and task artifacts; those files are never copied into product worktrees. Other tasks may start from the same target in their own worktrees while this task is active.

When implementation is clean and committed, run the read-only closure check:

```text
yy task preflight TASK_ID
```

Repair any reported admission, generated-output, runtime, or closure defect while
the task is still `WORKING`. Then run:

```text
yy task finish TASK_ID
```

Finish repeats the preflighted closure, validates the exact task identity and
committed tip, runs configured focused validation, and records the task as
`QUEUED`. It does not launch review, merge, release, push, deploy, clean up, or
synchronize controller and product branches. Use `yy task status TASK_ID` for
bounded read-only observation.

Use `yy merge status TASK_ID` for read-only observation. The target owner runs `yy merge land TASK_ID` for exactly one immutable source and automatic Ledger projection; `yy merge project TASK_ID` is the idempotent recovery for projection failure or stale Ledger truth. A moved target requires recomposition and renewed candidate checks. Preserve private conflict bytes; a conflicting task cannot block an unrelated task.

Tests and semantic reviews remain explicit project checks outside merge. The native adapter launches zero models, chooses no reviewer, schedules no suite, and owns no repair or evidence-cache loop. Git ancestry is not a test/review/requirements claim. Integrate release-version changes through this same ordinary task/native-Git lifecycle; package preparation is maintainer-only and release, push, publish, deploy, and cleanup remain separate authorities outside `yy`.
