# Workflows

## Define a workflow

A workflow is a plain Python file containing only definitions. See
`examples/workflow.py`:

```python
import sys

WORKFLOW = "mydag"
SCHEDULE = ["hourly"]  # OnCalendar= expressions, see systemd.time(7)


def fetch():
    print("Fetching data", file=sys.stderr)


def transform():
    print("Transforming data", file=sys.stderr)


tasks = {
    "fetch": fetch,
    "transform": transform,
}

dag = {
    "fetch": set(),
    "transform": {"fetch"},
}
```

`WORKFLOW`, `tasks` and `dag` are required. `SCHEDULE` is optional for running
directly and required for installing. Modules next to the file are importable,
including from inside tasks. Each load keeps its own sibling-module cache, so
separate workflows can use the same helper-module names without sharing code
or state. Lazy schedule and prerequisite iterators are consumed under the same
sibling-import isolation as the definition itself.

Every task must appear in the DAG, including tasks with no prerequisites.
Tasks must be synchronous callables: async functions are rejected during loading,
and a synchronous callable that returns an awaitable fails its task rather than
being reported successful. Floe does not await or schedule async work.
Task names must be nonempty strings. Workflow names must be 1–247 ASCII
characters, start with a letter or digit, and contain only letters, digits,
`_`, `.` or `-`; they are also the systemd unit names. Floe validates the whole
definition when loading it and reports every problem at once (missing or
unknown tasks, cycles, non-callable tasks, bad names or malformed schedule
values), before any task runs. When planning an installation, including
`install --dry-run`, Floe also parses every calendar expression with
`systemd-analyze` before either unit can be written. Direct execution does not
need systemd tools.

By default tasks run one at a time. Without a retry policy, an exception stops the workflow,
records failure, and returns a nonzero exit code. Tasks following the failure are not executed,
including independent tasks later in the execution order.

## Run independent tasks in parallel

The optional `@parallel` marker makes a function eligible to overlap with other
marked tasks when installed in parallel mode. It leaves the function unchanged. For example:

```python
from floe import parallel

@parallel
def fetch():
    return 42
```

Register `fetch` in `tasks` as usual. The mark belongs to the function, not to
one dictionary entry: aliases of that function are marked in newly constructed
workflows. Existing workflows keep their snapshot. To give two registrations
different eligibility, decorate a separate function that calls the shared helper.
Then opt in when installing: `floe install flow.py --workers 2`. A limit of 1
(the default for new installs) uses the existing in-process runner. Reinstalling
without `--workers` retains the recorded limit; `--workers 1` disables parallel
mode. Direct `floe run` and `floe.run()` remain sequential. `@parallel` does not
remove dependencies. An unmarked task waits for active work to drain and runs
alone. The limit caps active task executions, including those draining on exit;
short tasks and mixed workflows may not speed up.

Parallel mode requires a delegated user systemd service, Linux cgroup v2 with
`cgroup.kill`, and Python/Linux support for pidfds and memfd. It fails instead of
silently dropping containment. The supervisor loads the workflow, and each
worker loads it again before calling its task. Keep the definition and helpers
stable throughout the run. Do not rely on top-level code running only once or
on global mutations propagating to another task. Use [result bindings](#pass-small-results-to-downstream-tasks)
or files instead. Parallel installations reject workflows with retry policies;
sequential retries are unchanged. Each task group is a cleanup boundary for
trusted, cooperative code, not a sandbox.

While a parallel run is active, `floe cancel NAME --task A` hard-stops A and its
task group. Tasks that require A's success are skipped; unrelated branches can
continue. `floe cancel NAME` hard-stops active tasks and prevents new ones from
starting. A successful response acknowledges the request, not completion; check
`floe runs` or `floe logs` for final state. A task request is rejected if the task
isn't active or was already requested. Ordinary failure stops new launches but
lets active peers finish; failed takes precedence over cancelled. A cancelled
run is not a successful start.

A result is available only after successful exit, output drain and task-group
removal. After worker exit, output and descendants get up to two seconds to
finish; on timeout Floe kills the group, fails the task and withholds its result.
Systemd bounds the whole service separately. External termination without a
terminal event is incomplete, not evidence of cancellation or success.

stdout carries only Floe's events. Definition/import output is routed to stderr
at the CLI boundary, including subprocess and raw file-descriptor writes. This
also keeps `floe list --check --json` parseable when checking noisy definitions.
During sequential task execution, `floe run` sends task stdout to stderr, whether from
`print()` or child processes, and flushes Python's output before each event so
that output stays
with the task that wrote it. End output lines with a newline: under systemd,
stdout and stderr share one journal stream, so an unterminated line is joined
to the next event, which history then cannot recognize.

## Pass small results to downstream tasks

Set `inputs[consumer][keyword] = producer` to pass a producer's return value to a
consumer in either execution mode. For example, in a definition with `WORKFLOW = "inventory"`:

```python
def prices():
    return {"apple": 2}

def quantities():
    return {"apple": 3}

def total(*, prices, quantities):
    print(prices["apple"] * quantities["apple"])

tasks = {"A": prices, "B": quantities, "C": total}
dag = {"A": set(), "B": set(), "C": set()}
inputs = {"C": {"prices": "A", "quantities": "B"}}
```

`inputs` is now a reserved definition-file global; rename any unrelated helper
variable with that name before upgrading. Each binding adds a prerequisite; don't repeat it in `dag`. An ordinary DAG
edge waits without passing a value. A bound consumer must accept the named
keyword arguments. Only a successful producer
supplies its value; a failed prerequisite prevents the whole call, not just
one argument. `None` is a valid result, not a missing value. Printing is still
logging. A synchronous return nobody binds is ignored as before.

Bound results must be built-in JSON values: `None`, bool, int, finite float,
str, list, or dict with string keys, with no cycles. Each argument is a private
copy, including on each retry. Results live only in this run; Floe doesn't put
them in events or history. Invalid results fail the producer and are not
retryable task exceptions. Size limits: 1 MiB per result, 8 MiB total retained,
measured as compact ASCII-escaped JSON, at most 64 levels below the root.
Python's integer-to-string conversion limit applies. Floe releases a stored
value after its last bound consumer. For larger data, return a path string to a
file that your task writes and manages.

## Run directly

```bash
floe run examples/workflow.py
```

Events are printed to stdout as JSON lines and task output to stderr. Direct
execution does not store these events in journald.

Each run emits `run_started`, `task_started`, `task_completed`, and
`run_completed` events. Every event includes a schema version, workflow name,
UTC timestamp, and run ID. The run ID uses systemd's `INVOCATION_ID` when
available, otherwise a generated UUID. An invalid definition fails before
`run_started`, so it produces an error message but no events.

While a run executes, the environment variable `FLOE_RUN_ID` holds its run ID,
for tasks and the child processes they start. Use it to label files or records
that a run produces; under systemd it matches the ID that `floe runs` shows.

## Opt into task retries

Define an optional `retries` mapping without changing `tasks` or `dag`:

```python
from floe import RetryPolicy

retries = {
    "fetch": RetryPolicy(
        max_attempts=3,           # includes the initial call
        delay_seconds=5,         # fixed, finite, nonnegative; zero is allowed
        retry_on=(TimeoutError, ConnectionError),
    ),
}
```

Only named tasks retry; other tasks still run once. `max_attempts` must be a
positive integer. `retry_on` must explicitly name a nonempty sequence of ordinary
`Exception` subclasses. Select the exceptions your task's libraries actually
raise. `(Exception,)` deliberately opts into broad matching, including programming
errors; prefer specific transient failures. Malformed policies and unknown task
names are reported alongside other definition errors, before execution.

A retry calls only the failed task again, in the same process, retaining its
in-memory and sibling-module state. Successful prerequisites are not rerun.
Dependents wait for success. When attempts are exhausted or an exception is not
retryable, the workflow fails and no later tasks run, including independent ones.
There is no wait after the final attempt. Interrupts/cancellation are not retried;
a cancellation during a retry wait stops without another attempt. Callback and
Floe synchronous-return validation failures are never retried as task exceptions.

Retries can repeat side effects; tasks must be safe to repeat. There is **no
exactly-once guarantee**, rollback, fresh process, or automatic state reset.
The deployed service's workflow timeout bounds attempts and waits together;
direct `floe run` still has no overall timeout. Bound individual external calls.
There are no workflow-wide retry defaults, exponential backoff, jitter, retries
of whole workflows, or systemd `Restart=` retries.

Retry-enabled workflows emit schema-2 attempt events; unconfigured workflows keep
the existing schema-1 sequence. Failed attempts remain in history even after
success. See [History](history.md#attempt-history-and-json-compatibility).

[`examples/retries.py`](../examples/retries.py) demonstrates eventual success with
no external calls or real retry delay.

## Write workflows that run unattended

Scheduled runs happen while nobody watches. These patterns keep them predictable:

- **Pass large or durable data through files.** Write those results to files, for example in a `state/` directory next to the
  workflow file, and replace each file atomically (write a temporary file in the
  same directory, then `os.replace` it) so a crash never leaves half a file.
- **The next run starts afresh.** After final failure, the next scheduled run starts
  from the beginning, whether or not tasks have retry policies. Make tasks safe to repeat: record what was done, keyed by
  a stable identity such as an item ID, and skip it next time.
- **Save results before acting on them.** When computing a result is expensive
  or not repeatable, store it first; a run that fails while acting then leaves
  the next run something to act on instead of computing it again.
- **Check prerequisites first.** A task that checks required tools and
  credentials, with the tasks that need them depending on it, fails clearly
  instead of halfway through.
- **Finish independent work, then fail.** When one of several independent
  sources fails, record the failure, process the rest, and raise in a final task
  so that `floe runs` shows the run as failed, with the reason.
- **Bound external calls.** Give network requests and child processes their
  own timeouts; the service's `TimeoutStartSec=30min` stops the whole run.
- **Label what a run produces** with `FLOE_RUN_ID`, so files and records can
  be traced back to `floe runs` and `floe logs`.

`examples/checksums/` applies these patterns: it reports files added,
changed, or removed in a directory since the last run, keeps going when a file
cannot be read, and rejects state written by an overlapping run.

[`examples/feedwatch/`](../examples/feedwatch/README.md) applies them to an
agent: it asks [pi](https://pi.dev) to triage new feed items, treats both the
feed content and the agent's reply as untrusted, and saves each decision before
publishing it. It needs pi and a model login, unlike the rest of Floe.
