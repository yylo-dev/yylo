# Clean Bolt task workspaces

The metadata controller owns Kanban, task state, and compact artifacts. Product
code lives only in the target branch and dedicated feature worktrees.

`TASK_ROOT` names the canonical controller. Control-plane routing never switches or cleans the checkout where the user invoked `yy`.

Independent agents and reviewers use fresh `yy pi` contexts. Bare `pi` and indirect provider/model overrides are forbidden.

1. Start each selected feature with `yy task start TASK_ID`. The command records
   the exact target SHA and creates one task branch/worktree from it.
2. Enter the returned worktree and, before editing or testing, follow the
   exact-lock, validation-cwd-aware [task dependency hydration](../wiki/controller/task_dependency_hydration.md)
   contract. Stop before implementation on provisioning or clean-tree failure.
3. Implement, run focused tests, and commit only inside the returned worktree.
   Starting feature Y never waits for feature X; each has its own worktree.
4. Run the read-only `yy task preflight TASK_ID` after the worktree is clean
   and committed. Repair any closure defect while the task remains `WORKING`.
5. Run `yy task finish TASK_ID` against that exact preflighted tip. This
   validates affected paths/tests and queues the immutable feature tip.
6. Observe with `yy merge status|arbiter status`. One fenced target owner runs
   `yy merge arbiter run` or typed `yy merge drive` and exits when idle/blocked;
   agents never poll. `next|resolve` are explicit recovery mutations. Preserve
   dirty conflict bytes for one bounded managed repair; expiry is not takeover.
7. Implementation workers never launch lifecycle-semantic reviewers. The
   managed merge queue is the sole lifecycle-semantic review owner: low risk gets
   zero, normal risk
   gets at most one, and high risk gets Reviewer A then Reviewer B on one frozen
   candidate. It permits at most one repair candidate; a second material finding
   terminalizes as `REVIEW_FINDINGS_EXHAUSTED`, with no third autonomous review
   or silently created repair task.
8. Release-version changes follow the same ordinary task/merge lifecycle.
   Maintainer package preparation remains separately authorized outside `yy`.
9. After expected-SHA CAS, verify identity/readback only; do not redispatch a
   semantic reviewer for byte-identical delivery.
10. Cleanup is reachability-safe. Push, release, publish, deploy, production
   mutation, restart, and post-deploy E2E always require separate authority.

Do not copy controller ledgers/specs/artifacts into product worktrees, author
helper receipts, or synchronize controller and product histories.
