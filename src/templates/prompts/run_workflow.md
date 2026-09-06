# Run a workflow or Bolt task

Choose one public interface by intent:

`TASK_ROOT` names the canonical controller. Control-plane routing never switches or cleans the checkout where the user invoked `yy`.

- Generic ordered reporting or agent work: `workflow_runner.sh --workflow PATH`.
- Feature implementation: `yy task start TASK_ID`; immediately and before editing/testing hydrate exact-lock dependencies for configured validation roots using [task dependency hydration](../wiki/controller/task_dependency_hydration.md), and stop before implementation on failure; implement, test, and commit; run read-only `yy task preflight TASK_ID`; then `yy task finish TASK_ID` on that exact tip.
- Delivery observation: `yy merge status|arbiter status` (read-only).
- Delivery mutation: one fenced `yy merge arbiter run` or typed `yy merge drive`;
  `next|resolve` are explicit recovery primitives, not a polling loop.

Generic Workflow Runner remains available, including read-only doctor support
for historical local-integration artifacts. It must not execute or adapt retired
feature-integration manifests.

Task state is durable in the metadata controller. Product workers receive only
their dedicated product worktree; controller data is not copied there. Multiple
features may be implemented concurrently, while one per-target merge queue owns
the short expected-SHA mutation window.

Implementation workers never launch lifecycle-semantic reviewers. The managed
merge queue is the sole lifecycle-semantic review owner: low risk gets zero,
normal risk gets at
most one, and high risk gets Reviewer A then Reviewer B on one frozen candidate.
It permits at most one repair candidate; a second material finding terminalizes
as `REVIEW_FINDINGS_EXHAUSTED`, with no third autonomous review or silently
created repair task. Post-merge work is deterministic identity/readback, not a
second review. Version changes use the same ordinary task/merge lifecycle;
maintainer package preparation, release, and external publication remain outside
`yy` and separately authorized.
