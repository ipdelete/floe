# Parallel task execution: proposed first release

**For maintainer review, not an accepted API.** [Issue #8](https://github.com/ipdelete/floe/issues/8) · [decision record](decisions.md). Floe still runs sequentially. The design question is whether to offer a deliberately different, opt-in execution mode rather than change what existing tasks mean.

## Where it runs

Keep concurrency 1 on the existing in-process path. For a first parallel release, propose **file-backed, installed workflows only**, with an explicit positive worker limit and a delegated cgroup v2 service. Keep one systemd service invocation per workflow run, not one service per task. The manager still owns scheduling, no-overlap, and the overall timeout. Only installations opting in request delegation. Give the service a distinct internal supervisor entry point: today's service uses `floe_cli run`, which must not implicitly switch behavior just because it inherited a writable cgroup.

Direct CLI and library callers retain sequential execution. Reject an *explicit* parallel request on unsupported paths before any task starts; don't silently serialize, escalate privileges, or move a caller into a service. If a delegated subtree, `cgroup.kill`, or required kernel facilities are unavailable at run time, produce a diagnostic and failed run, not weaker containment. Installed-only is a scope recommendation, not a Linux limitation: a separately managed direct runner is a future alternative.

## One run, two responsibilities

- **Supervisor:** owns the run ID, worker slots, dependency outcomes, control channel and all lifecycle events. Use `TopologicalSorter` for readiness; `done()` means processed, not succeeded. Launch only after *every* prerequisite succeeded. Record skipped nodes and advance traversal without creating a worker for them.
- **Worker:** one fresh interpreter per task execution. Enter a newly owned task cgroup before loading the trusted definition by path and looking up the task key. This repeats top-level code and loses cross-task Python mutations. Opt-in workflows must communicate via explicit external state, not shared objects; definitions must be repeatable and stable across the run. Programmatic callables with no source file stay sequential. Do not fork arbitrary user interpreter state or enable a subreaper in a library host.
- **Completion:** a worker exit code, output EOF, and an empty cgroup answer different questions. Require a matching terminal control record, successful worker exit, drained output, and no surviving task descendants before releasing a successful dependency. Proposed policy for a worker that leaves descendants: a bounded drain period within the workflow deadline, then kill the owned group and fail the task; never wait indefinitely or release a success early. Bounds and reaping responsibility must be specified and tested before enabling this mode. The controlled probe distinguished these observations; it did **not** implement this production policy.

## Cancellation, failure and reporting

| Trigger | Active work | New launches | Proposed reported outcome |
| --- | --- | --- | --- |
| Explicit cancel of running A | Hard-cancel A's group; confirm termination and cleanup | Skip descendants requiring A's success; continue unrelated branches | A cancelled; skipped tasks distinguished from unobserved ones; run not successful |
| Explicit cancel of this run | Supervisor latches stop-launching before killing owned active groups | None, including already-ready roots | Run cancelled if confirmed; missing evidence remains incomplete |
| Ordinary task failure after retries, if enabled | Drain active peers | No more tasks once failure is observed | Run failed; retain peer results |

Propose failure takes precedence over cancellation if both occur; a worker killed for an unknown reason is **not** proof of an explicit cancel. Use execution identity and supervisor-serialized acceptance of requests to resolve cancel-vs-completion races. An acknowledgement of receipt is not a terminal result. Start with hard cancellation, not a promise of graceful task shutdown.

For user-triggered cancellation, propose a local, owner-only control endpoint tied to the live service invocation, not a CLI that writes a caller-supplied cgroup path. Specify request identity (invocation and task execution), authorization, stale/repeated requests, pending/terminal tasks and supervisor death before shipping a command. The probe injected requests internally; no protocol has been validated.

## Events and retries

Only the supervisor emits public events. Keep control records separate from output bytes. Today's history reader assigns ordinary lines to the most recently started task/attempt; that is invalid for concurrent tasks. Introduce and validate explicit run/task-execution/attempt identity for output and events. Keep old journal schemas readable; never upgrade missing evidence to success. Exercise raw writes, child output, partial lines and bounded buffering while draining all pipes. Define retry-boundary output ownership before enabling parallel retries: inherited descriptors can outlive an attempt. Do not silently disable retries, retry cancellation, or repeat non-task failures. If retry support is staged later, reject the unsupported combination explicitly.

## Validation and release gates

1. Test the scheduler without systemd: bounds 1/2, join, transitive skips, independent queued root at capacity, failure drain, simultaneous completions, task/workflow cancellation, and idempotent late requests. Sequential defaults retain shared state, output, retry and public API contracts.
2. Test the worker boundary locally: file loading and repeat side effects, isolated imports, partial launch failure, explicit identity under interleaved output, long lines, retry boundaries, premature EOF/worker death. Include history, CLI JSON and dashboard readers. A new task status alone is not an end-to-end implementation.
3. In an explicitly approved disposable systemd environment, validate the actual installed service command, ownership and delegation, repeated installs, no overlap, timeout, supervisor SIGKILL, independent canary survival, and recoverable cleanup. [#16](https://github.com/ipdelete/floe/issues/16) remains a gate: killing fixture groups with a *live* supervisor did not test manager cleanup after supervisor death. Never target units/cgroups by a user-supplied raw path or claim isolation from hostile same-user tasks.
4. After approval, open a scoped implementation issue. Suggested first slice: internal readiness/outcome state machine with fake workers and the two-branch cancellation matrix; no public parallel switch until execution, history, control and lifecycle gates are satisfied. Ship neither the spike scripts nor a placeholder cancellation command.
