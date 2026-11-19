# Parallel task execution: proposed first release

**Design direction approved; proposed syntax is not a shipped API.** [Issue #8](https://github.com/ipdelete/floe/issues/8) · [decision record](decisions.md). Floe still runs sequentially. The design question is whether to offer a deliberately different, opt-in execution mode rather than change what existing tasks mean.

## Author contract: eligibility, order, and data are separate

Proposed spelling, **not available today**:

```python
from floe import parallel

WORKFLOW = "inventory"

@parallel
def fetch_prices():
    return {"apple": 2}

@parallel
def fetch_quantities():
    return {"apple": 3}

def calculate_total(*, prices, quantities):
    return sum(price * quantities[item] for item, price in prices.items())

tasks = {"A": fetch_prices, "B": fetch_quantities, "C": calculate_total}
dag = {"A": set(), "B": set(), "C": set()}  # any additional order-only edges go here
inputs = {"C": {"prices": "A", "quantities": "B"}}
```

- `@parallel` marks a callable **eligible** to overlap with other marked tasks. It does not submit work, wrap a return value in a future, or remove a prerequisite. Keep `tasks` as the registry. Capture marker metadata and the original signature when loading; sibling-import wrappers must preserve both. Marked and unmarked tasks can both produce and consume results.
- The worker limit is a ceiling, not eligibility. Limit 1 runs serially, even with marks. In managed parallel mode an unmarked task is **exclusive**: stop admitting workers when it is selected, drain active workers, then run it alone before admitting more. A mark-free definition at the default limit behaves as today. Exclusivity is a scheduling guarantee, not shared interpreter memory. Explain the drain in author documentation and expose effective eligibility and limit when inspecting a loaded definition (never load one for metadata-only inspection). A mixed graph can run nearly serially; no speedup is promised.
- `inputs[consumer][keyword] = producer` binds that producer's **whole returned value** to a keyword argument. The effective prerequisites are the union of bindings and `dag` edges; a repeated edge counts once. An order-only edge passes nothing. In the example C is called with `prices={"apple": 2}, quantities={"apple": 3}` after A and B succeed, regardless of completion order. There is no implicit binding by task name or parameter position.
- Normalize and snapshot both mappings before graph validation. Reject unknown names, self-dependencies and cycles (including mixed edge kinds). For a bound consumer require an inspectable signature that can be called with the declared keywords, including required arguments; reject positional-only requirements and unknown keywords without `**kwargs`. Aggregate independent definition problems before running tasks. Do not retroactively require signature inspection of legacy no-binding tasks.

## Result contract (proposed v1)

Use the same value contract at worker limit 1 and across processes. Capture a return value **only when another task binds it**. Otherwise preserve current synchronous-return validation and ignore the value. Do not publish stdout as a result. For bound values propose finite JSON data: null, booleans, strings, integers, finite floats, lists and string-keyed dictionaries recursively; reject unsupported or cyclic objects, rather than silently coercing tuples, keys or dates. No pickle. For large artifacts pass a path string to a file published by the task; Floe does not copy, validate or delete that file.

The supervisor owns a run-local result table. Stage bounded, length-framed bytes over a private data/control channel; never put payloads into journal events, logs, command arguments or environment variables. Admit a result only when its producing execution has committed success. Retain immutable encoded bytes until the last bound consumer no longer needs them; decode a private value for each call/attempt so one consumer's mutation cannot affect another. Bound both individual frames and total retained bytes per run; fail explicitly if a bound is exceeded (limits to settle before release). Reject malformed or missing frames as errors; successful `None` is an explicit JSON null, **not** absence. Result validation/transport failure is not a retryable task exception. Stage/discard by run, execution and attempt: a failed, cancelled or superseded attempt cannot supply inputs. Do not persist results across runs or promise to undo a task's external side effects. These are design proposals, not findings of the process spike.

## Where it runs

Keep concurrency 1 on the in-process path; add binding support there without changing legacy zero-argument calls. For a first parallel release, propose **file-backed, installed workflows only**, with an explicit positive worker limit and a delegated cgroup v2 service. Keep one systemd service invocation per workflow run, not one service per task. The manager still owns scheduling, no-overlap, and the overall timeout. Only installations opting in request delegation. Give the service a distinct internal supervisor entry point: today's service uses `floe_cli run`, which must not implicitly switch behavior just because it inherited a writable cgroup.

Direct CLI and library callers retain sequential execution. Reject an *explicit* parallel request on unsupported paths before any task starts; don't silently serialize, escalate privileges, or move a caller into a service. If a delegated subtree, `cgroup.kill`, or required kernel facilities are unavailable at run time, produce a diagnostic and failed run, not weaker containment. Installed-only is a scope recommendation, not a Linux limitation: a separately managed direct runner is a future alternative.

## One run, two responsibilities

- **Supervisor:** owns the run ID, worker slots, dependency outcomes, control channel and all lifecycle events. Use `TopologicalSorter` for readiness; `done()` means processed, not succeeded. Launch only after *every* prerequisite succeeded. Record skipped nodes and advance traversal without creating a worker for them.
- **Worker:** one fresh interpreter per task execution. Enter a newly owned task cgroup before loading the trusted definition by path and looking up the task key. This repeats top-level code and loses cross-task Python mutations. In managed mode use this worker boundary for exclusive as well as marked tasks so the supervisor remains able to cancel them. Exchange data via declared results or explicit external state, not shared objects; definitions must be repeatable and stable across the run. Programmatic callables with no source file stay sequential. Do not fork arbitrary user interpreter state or enable a subreaper in a library host.
- **Completion:** a worker exit code, output EOF, and an empty cgroup answer different questions. Require a matching terminal control record, successful worker exit, drained output, and no surviving task descendants before releasing a successful dependency. Proposed policy for a worker that leaves descendants: a bounded drain period within the workflow deadline, then kill the owned group and fail the task; never wait indefinitely or release a success early. Bounds and reaping responsibility must be specified and tested before enabling this mode. The controlled probe distinguished these observations; it did **not** implement this production policy.

## Cancellation, failure and reporting

| Trigger | Active work | New launches | Proposed reported outcome |
| --- | --- | --- | --- |
| Explicit cancel of running A | Hard-cancel A's group; confirm termination and cleanup | Skip descendants requiring A's success; continue unrelated branches | A cancelled; skipped tasks distinguished from unobserved ones; run not successful |
| Explicit cancel of this run | Supervisor latches stop-launching before killing owned active groups | None, including already-ready roots | Run cancelled if confirmed; missing evidence remains incomplete |
| Ordinary task failure after retries, if enabled | Drain active peers | No more tasks once failure is observed | Run failed; retain peer results |

Acceptance matrix for `A → C` and `B → D` with **bound inputs**, cancelling while A and B are running:

| Request | A / B | C receives A | D receives B |
| --- | --- | --- | --- |
| None | Both succeed | Runs with A's exact value | Runs with B's exact value |
| Cancel A | A cancelled; B succeeds | Skipped; never invoked | Starts after B succeeds with B's exact value |
| Cancel workflow | Both cancelled | Skipped; never invoked | Skipped; never invoked |

For the join in the example, cancel A and let B succeed: C never receives partial kwargs or a substituted null. The commit point is one serialized supervisor transition, **after** it verifies terminal success, required result validation, worker exit, output drain and group emptiness in managed mode. In that transition publish the staged value and mark the task succeeded before releasing dependents. Cancellation accepted before that transition wins, including during drain; discard even an already-buffered return frame. After commit a late request cannot retract the value. Test both event orders. The spike exercised process outcomes, **not** result delivery.

Propose failure takes precedence over cancellation if both occur; a worker killed for an unknown reason is **not** proof of an explicit cancel. Use execution identity and supervisor-serialized acceptance of requests to resolve cancel-vs-completion races. An acknowledgement of receipt is not a terminal result. Start with hard cancellation, not a promise of graceful task shutdown.

For user-triggered cancellation, propose a local, owner-only control endpoint tied to the live service invocation, not a CLI that writes a caller-supplied cgroup path. Specify request identity (invocation and task execution), authorization, stale/repeated requests, pending/terminal tasks and supervisor death before shipping a command. The probe injected requests internally; no protocol has been validated.

## Events and retries

Only the supervisor emits public events. Keep control records separate from output bytes. Today's history reader assigns ordinary lines to the most recently started task/attempt; that is invalid for concurrent tasks. Introduce and validate explicit run/task-execution/attempt identity for output and events. Keep old journal schemas readable; never upgrade missing evidence to success. Exercise raw writes, child output, partial lines and bounded buffering while draining all pipes. Define retry-boundary output ownership before enabling parallel retries: inherited descriptors can outlive an attempt. Do not silently disable retries, retry cancellation, or repeat non-task failures. If retry support is staged later, reject the unsupported combination explicitly.

## Validation and release gates

1. Test the scheduler without systemd: bounds 1/2, join, transitive skips, independent queued root at capacity, failure drain, simultaneous completions, task/workflow cancellation, and idempotent late requests. Sequential defaults retain shared state, output, retry and public API contracts.
2. Test bindings at limits 1 and 2: join with producers finishing in reverse order, order-only edges, fan-out mutation, explicit null, failed/retried/cancelled producer, mixed-edge cycle, wrong signature, oversized/invalid values and total retention cap. Assert exact inputs and zero calls to skipped consumers in the matrix above. Instrument marked/exclusive boundaries to assert real overlap and exclusivity, not just launch order. Test the worker boundary locally: file loading and repeat side effects, isolated imports, partial launch failure, explicit identity under interleaved output, long lines, retry boundaries, premature EOF/worker death. Include history, CLI JSON and dashboard readers. A new task status alone is not an end-to-end implementation.
3. In an explicitly approved disposable systemd environment, validate the actual installed service command, ownership and delegation, repeated installs, no overlap, timeout, supervisor SIGKILL, independent canary survival, and recoverable cleanup. [#16](https://github.com/ipdelete/floe/issues/16) remains a gate: killing fixture groups with a *live* supervisor did not test manager cleanup after supervisor death. Never target units/cgroups by a user-supplied raw path or claim isolation from hostile same-user tasks.
4. After approval, open a scoped implementation issue. Suggested first slice: model and validate bindings, then exercise readiness, result publication and the cancellation matrix with a serial backend and fake workers; no public parallel switch until execution, history, control and lifecycle gates are satisfied. Ship neither the spike scripts nor a placeholder cancellation command.
