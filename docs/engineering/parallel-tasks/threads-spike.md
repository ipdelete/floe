# Spike: ordinary thread-based task workers

**Status:** investigation stopped after contract blockers; recommendation awaits
maintainer judgment. No production implementation or approved API change.

Issues: [#9](https://github.com/ipdelete/floe/issues/9), parent
[#8](https://github.com/ipdelete/floe/issues/8); retry work
[#6](https://github.com/ipdelete/floe/issues/6). See [D-001](decisions.md#d-001-do-not-advance-ordinary-thread-pool-workers-to-production-design-yet).

## Decision and scope

Can an ordinary in-process thread pool provide useful task concurrency while
preserving Floe's supported import/output/lifecycle contracts? Competing
hypotheses were low-overhead I/O concurrency versus serialization or incompatible
restrictions imposed by shared process state.

The user explicitly declined a proposed 30-minute timebox and asked to proceed
without one. We instead used the issue's early-stop criterion: investigate the
smallest decision-relevant contract questions before building a scheduler or
benchmark suite. The work did not redesign the loader or implement retries.

**Recommendation:** do not advance this thread-pool approach to production design
under the current contracts. Investigate processes in #10, then compare approaches.
This is not approval of processes, a decision to abandon parallelism entirely, or
a claim that every imaginable thread architecture is impossible.

## Setup and reproduction

Baseline checkout: `d7df8c727d00bfd1e350fed49060b69c48ee3735`.
Observed environment:

- Python 3.14.4, GCC 15.2.0.
- Linux `6.18.58.1-microsoft-standard-WSL2`, aarch64, glibc 2.43.
- `Py_GIL_DISABLED=0`, `sys._is_gil_enabled() == True`.

From the repository root, with the project's `uv` environment:

```bash
uv run python docs/engineering/parallel-tasks/thread-probes.py
```

[The probe script](thread-probes.py) is reproducible evidence, **not a parallel
runner or a production test fixture**. It uses two-worker executors and event
handshakes to force specific interleavings, not sleeps to hope for a race.
Sequential positive controls validate the helper and output setups first.
Each probe uses a separate interpreter/process group, temporary files, bounded
waits and an outer deadline. The cancellation probe explicitly releases its task
and terminates/reaps only the child it created. The harness kills its own probe
process group if waiting raises (including the outer deadline) as a safeguard.
No service-manager or journal calls occur, and no user session is modified.

The import probe intentionally exercises the documented unsupported concurrent
use of task wrappers in a disposable interpreter. It is not a regression in the
supported sequential API. Run the entire script for isolation, rather than
importing a probe into a long-lived application.

## Observations

### 1. Existing task wrappers are not concurrently composable

Relevant code: `src/floe/loader.py`, `_WorkflowImports.active()` and `wrap()`.
Loading and execution activate a workflow-specific sibling-module cache by
replacing matching entries in process-global `sys.modules` and temporarily
prepending the workflow directory to `sys.path`. Every wrapped call restores
its own entry snapshot on exit.

The probe loads **one workflow**, with two task names sharing its import cache.
An ambient module with the same helper name makes a wrong import observable.
Sequential task calls see the workflow helper and retain its counter across
calls (1, then 2). Sequential exceptions also restore the ambient module and path.

Forced concurrent order:

1. A enters its wrapper, imports the workflow helper and waits.
2. B enters its wrapper, imports the same workflow helper and waits.
3. A exits, restoring its snapshot of the ambient module/path.
4. B imports the helper again while still inside its task, then exits.

| Observation | Serial control | Overlap, B succeeds | Overlap, B raises |
| --- | --- | --- | --- |
| B's helper during late import | workflow | **ambient** | **ambient** |
| Original `sys.path` restored after both calls | yes | **no** | **no** |
| Ambient helper identity restored | yes | **no** | **no** |

B snapshots A's active import state, not the actual ambient state. A's exit
removes B's active environment; B's exit then restores a stale in-task snapshot.
The outcome was reproduced in both normal and exception exits. No second
workflow, simultaneous definition load, or user mutation of `sys.path` was
needed to expose it.

**Inference:** a lock covering the entire current `active()` context would
serialize whole task calls, because the context surrounds arbitrary task code.
Locking just entry/exit would still allow the demonstrated invalid nesting.
A run-scoped import context for one workflow might avoid this particular race,
but requires different wrappers/lifetimes and does not solve the output issue.
We did not implement or test that alternative. Existing restrictions on unrelated
threads importing same-named sibling modules still matter.

### 2. Python-local output routing does not localize file descriptors

Relevant code: `src/floe_cli/main.py`, `_stdout_on_stderr()`/`_run()`, and
`src/floe/history.py`, `_log_lines()`.

The current CLI separates event stdout from task output at a process-wide
boundary. Sequential history can associate output with the most recent task
and attempt events. With overlapping tasks, chronology cannot identify the
writer. A clean event stream alone would not fix attribution.

The probe tests a possible thread-local Python stream proxy plus per-task
`dup2()` redirection. This is a deliberately small candidate, **not** a change
to the CLI and not an end-to-end CLI/journal test. A writes newline-terminated
output from `print()`, `os.write(1, ...)`, and a fresh Python child inheriting
stdout. B changes descriptor 1 after A's routing is installed, before A writes.

| Source written by A | Serial control | Overlap |
| --- | --- | --- |
| Python `print()` | A's Python sink | A's Python sink |
| Raw descriptor 1 write | A's descriptor sink | **B's descriptor sink** |
| Child's inherited stdout | A's descriptor sink | **B's descriptor sink** |

The positive Python result matters: thread-local routing works for cooperating
Python stream writes. It does **not** make descriptor 1 thread-local. Selecting
a different Python sink cannot change where a raw write or a child with inherited
stdout goes. The probe does not test descriptor 2 separately; the same shared
process descriptor-table concern applies by inference, not a second observation.

**Inference:** keeping a single shared output descriptor preserves separation
from events but loses automatic task identity for arbitrary raw writes. Swapping
it per task races as demonstrated. A lock spanning task execution and inherited
output would remove task concurrency; locking only known Python writes would
miss arbitrary writers. Explicit task-tagged output/subprocess APIs could be a
different contract, but cannot silently replace existing support for ordinary
prints, raw writes, and inherited child output. Arbitrary native instrumentation
or specialized Linux descriptor-table arrangements were not investigated.

### 3. Running futures and children survive future cancellation

A worker starts its own child, then blocks waiting on an event. Once the parent
knows it is running:

- `future.cancel()` returns `False`.
- `shutdown(wait=False, cancel_futures=True)` returns while the future is still
  running and the child is still alive.
- Explicit cooperative release allows the worker to terminate and reap its child;
  the probe asserts completion and the child's collected return code.

This is a blocked but intentionally releasable task, not a test of killing an
uncooperative thread. A shutdown/join timeout is not successful cleanup.

**Contract analysis:** ordinary futures do not provide safe forced termination
of arbitrary running Python task code. A cooperative cancellation token would
be a new task requirement, not a solution for an arbitrary blocking call.
This alone does not disqualify threads for the proposed failure policy: draining
already-running tasks intentionally waits for them. It does mean draining can
block until the whole-workflow timeout, and fail-fast cancellation is not a
free alternative. Killing the workflow process is separate from cancelling a
future.

The existing deployed service sets `TimeoutStartSec` and `KillMode=control-group`.
Whole-process/control-group termination could still bound a threaded workflow;
that is a source-based expectation, **not real-systemd evidence from this spike**.
Direct `floe run` already has no overall timeout; this investigation does not
introduce one or claim existing direct execution cleans up arbitrary descendants.

## Other shared-state and retry considerations (source analysis, not new probes)

- `FLOE_RUN_ID` is set/restored around the whole run in `runner.py`. Tasks in one
  concurrent workflow could share that value. It is not a task/attempt identity;
  setting per-task identity by mutating `os.environ` would be process-global.
- Retain the existing retry execution identity `(run_id, task, attempt)` from #6.
  Thread-local/context-local identity can label cooperating Python callbacks,
  but does not automatically label arbitrary descriptor writes or subprocesses.
  No identity API or event-schema change is proposed here.
- Definitions and sibling-module state persist across tasks/attempts today.
  Threads share Python objects; the GIL does not turn a task's multi-step mutable
  state changes into transactions. Requiring all tasks/helpers to be thread-safe,
  or forbidding process-wide cwd/environment/import changes, would be new
  concurrency restrictions. Existing documentation recommends file-based task
  communication but does not promise every accepted callable is thread-safe.
- Tenacity currently owns bounded task retries in a sequential runner. A task in
  a retry delay must not release dependents as if successful. Whether waiting
  occupies a worker slot, and how cancellation interrupts waits across workers,
  remain design questions. This spike did not add or test parallel retries.
- This interpreter is GIL-enabled. No CPU-speedup claim is made for it or for
  free-threaded Python builds; neither a CPU workload nor a free-threaded build
  was benchmarked. Different GIL behavior would not make process-global import
  state or ordinary file descriptors task-local.

## Coverage against the shared evaluation contract

| Scenario | Evidence / remaining gap |
| --- | --- |
| Two independent tasks and dependent join; worker bound | Two tasks were forced to overlap in a two-worker executor. No DAG scheduler, dependent join, or scheduler-bound enforcement was implemented/tested. |
| Sibling imports, state restoration, failure | Serial controls plus deterministic same-workflow overlap on both success and exception paths; observed wrong helper and failed restoration. No redesigned loader or unrelated concurrent workflow test. |
| Prints, child stdout, raw writes, lifecycle events | All three output sources exercised with serial control; Python proxy succeeds while descriptor routing fails. Actual concurrent lifecycle stream parsing, journal attribution, partial lines and pipe backpressure not tested. |
| Failure while another task runs | Import probe raises after A finishes to inspect exception cleanup. Stop-launch/drain-running scheduling and alternative failure policies not implemented or measured. |
| Cancellation with task-owned child | Running future cannot be cancelled; child survives shutdown call. Explicit release and termination/reaping verified. SIGINT, hung native calls, forced process termination and systemd cleanup not tested. |
| Short/I/O/CPU workloads, memory/overhead | Not measured after contract blockers. No claim about useful speedup, worker-startup overhead, RSS, or free-threaded Python. |
| Installed no-overlap protection | Existing oneshot service configuration inspected only; no service-manager calls or real-session validation. |

The stop criterion in #8/#9 permits ruling out an approach without exhausting
all scenarios. Scheduler correctness and timing results would not repair the
observed import/output contract failures, so retaining a scheduler/benchmark
prototype would add code without changing this recommendation.

## Validation performed

- Probe script passed initially; the final script then passed **10 independent
  runs with identical JSON observations** (each run starts three probe interpreters).
- `uv run pytest -q`: **583 passed, 15 skipped**. The real-systemd suite remained
  opt-in and was not run.
- `uv run python examples/deploy_demo.py`: passed validation/planning only; no
  installation writes or service-manager calls.
- `git diff --check`: passed.
- These checks do not establish scheduler performance, process-worker viability,
  or real-systemd lifecycle behavior. Packaging/service commands were unchanged;
  no separate manual wheel-install exercise was needed for this evidence-only PR.

## What the evidence changes

A small `ThreadPoolExecutor` around today's tasks is not a safe incremental
implementation. Even two tasks of one workflow break import restoration, and
Python-local output identity is insufficient for the supported output sources.

A future thread proposal must identify how it solves arbitrary output attribution
and import lifetimes **before** scheduler/performance work is justified, or ask
for explicit compatibility changes. The next useful investigation is the process
spike, especially task loading/state continuity, output identity and descendant
cleanup—not production parallelism implementation. Neither approach has been
approved by these results.
