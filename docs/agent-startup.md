# Workspace-aware agent startup

Agent startup distinguishes an ordinary directory from an initialized or
registered Juno workspace before dispatching controller-owned preflight.

- **Ordinary directory or container parent:** generic agent behavior. No child
  directory search, controller creation, controller dependency checks, default
  project monitoring hooks, or installer hook. Explicit custom hooks remain
  user-owned. Merely naming a child `something_controller` grants no authority.
- **Registered controller/task:** the packaged shared resolver validates the
  persisted registration and role. Readiness uses the controller root; the
  agent and custom product hooks keep the intended invoking worktree cwd. No
  task-local resolver or installer script is required for startup.
- **Simple workspace:** retain its root-validated local agent and Ledger
  readiness semantics, not managed orchestration.
- **Unrelated inherited controller context:** refuse with a workspace diagnostic.
  Run from the registered controller/task, or use a shell without unrelated
  controller assertions for generic agent use. Do not copy scripts into a parent
  folder, select a child by name, or bypass a persisted registration failure.

Default START_RUN no longer invokes the dependency installer. The exact legacy
installer command is also excluded from effective runtime hooks without
rewriting persisted configuration. Dependency readiness is independent of the
optional-hook switch: it performs a bounded compatible-Ledger version probe and
checks existing package/runtime receipt compatibility. Failures remain blocking
before hooks, backend initialization, and agent dispatch. Installation, upgrade,
and receipt-bound runtime recovery are explicit maintenance operations, not
startup side effects. For an incompatible binding, inspect `yy scripts doctor`.

Startup refusals retain a typed error through the execution engine and are
rendered once by the CLI. Structured execution errors retain their message
rather than displaying an object coercion. A merge or successful ancestry check
does not substitute for these startup checks.

## Validation

Focused coverage includes real-Git task/controller registration with no local
scripts, ordinary and ambiguous-child parent folders, inherited unrelated
context, malformed registration, runtime/Ledger failures, Simple mode, custom
hook preservation, no backend launch on failed readiness, and structured error
messages. After building, `node scripts/test-controller-config-startup.mjs`
checks packaged migration plus controller/generic Pi execution with provider
dispatch stubbed, and the packaged CLI's single diagnostic on an unrelated
inherited controller. No model/network calls or package installations are used.
