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

Use `yy merge status` or `yy merge arbiter status` for read-only observation. The target owner starts one fenced on-demand `yy merge arbiter run` (or typed `yy merge drive`) and lets it exit when idle or blocked; agents do not poll or steal ownership on timeout. `yy merge next|resolve` are explicit diagnostic recovery mutations, not the ordinary session-driven loop. Dirty conflict bytes are preserved for one bounded managed repair.

The merge owner is the sole lifecycle-semantic review owner: low risk uses zero reviewers, normal at most one, and high exactly Reviewer A then Reviewer B on one frozen predecessor-bound candidate. One repair candidate and one delta review group are allowed; further material findings stop as `REVIEW_FINDINGS_EXHAUSTED`. Delivery uses expected-old-SHA CAS and deterministic readback. Integrate release-version changes through this same ordinary task/merge lifecycle; package preparation is maintainer-only and release, push, publish, deploy, and cleanup remain separate authorities outside `yy`.
