# Run a workflow or Bolt task

Choose one public interface by intent:

`TASK_ROOT` names the canonical controller. Control-plane routing never switches or cleans the checkout where the user invoked `yy`.

- Generic ordered reporting or agent work: `workflow_runner.sh --workflow PATH`.
- Feature implementation: `yy task start TASK_ID`; before editing or testing follow [task dependency hydration](../wiki/controller/task_dependency_hydration.md) and stop before implementation on failure; implement, test, and commit; run read-only `yy task preflight TASK_ID`; then `yy task finish TASK_ID` on that exact tip.
- Delivery observation: `yy merge status [TASK_ID]` (read-only).
- Delivery mutation: `yy merge land TASK_ID`, followed by the separate Ledger projection `yy merge project TASK_ID`.

Generic Workflow Runner remains available, including read-only doctor support
for historical local-integration artifacts. It must not execute or adapt retired
feature-integration or merge-drive manifests.

Task state is durable in the metadata controller. Product workers receive only
their dedicated product worktree; controller data is not copied there. Multiple
features may be implemented concurrently. Native Git serializes expected-old
updates to one target without FIFO admission: a private conflict for one task
cannot block an unrelated task.

Tests and semantic reviews are explicit project checks outside merge. The native
delivery adapter launches zero models, chooses no reviewer, schedules no suite,
and owns no repair or evidence cache. A moved target requires recomposition and
renewed exact-candidate checks. Git success is reported before Ledger projection;
a projection retry never repeats integration. Version changes use the same
ordinary task/native-Git lifecycle; maintainer package preparation, release, and
external publication remain outside `yy` and separately authorized.
