# Workflows

## Define a workflow

Save this as `hello.py`:

```python
WORKFLOW = "hello"
SCHEDULE = ["hourly"]


def greet():
    print("Hello!")


def finish():
    print("All done.")


tasks = {"greet": greet, "finish": finish}
dag = {"greet": set(), "finish": {"greet"}}
```

`tasks` gives each function a name. `dag` says what must run first: `greet` has no prerequisites, and `finish` runs after `greet`. List every task in both places. Floe checks the definition before running any task.

```bash
floe run hello.py
```

Tasks run one at a time by default. If a task fails, no later tasks run. Use normal functions, not `async def`; tasks can't return awaitables. You only need `SCHEDULE` if you [install the workflow](deployment.md).

Choose a workflow name that starts with an ASCII letter or digit. Use up to 247 characters: ASCII letters, digits, `_`, `.` or `-`. Each task name must be a nonempty string.

Floe imports the file as trusted Python code. Keep work inside task functions, not at the top level. Tasks can import helper modules beside the file. `floe run` writes events as JSON lines to stdout and task output to stderr. End each log line with a newline so it stays separate from Floe's events in the journal.

## Pass small results to downstream tasks

```python
def fetch():
    return 42


def report(*, count):
    print(f"Found {count} items")


tasks = {"fetch": fetch, "report": report}
dag = {"fetch": set(), "report": set()}
inputs = {"report": {"count": "fetch"}}
```

`inputs` tells Floe to run `fetch` before `report` and pass its return value as `count`. You don't need to repeat that prerequisite in `dag`. A `dag` entry alone sets the order without passing a value.

Use built-in JSON values, including `None`, with string keys and no cycles. Each consumer gets its own copy. Invalid results fail the producer. Limits are 1 MiB per result, 8 MiB total retained, and 64 levels below the root. Results stay in the current run and aren't logged. For larger results, write a file and return its path as a string. `inputs` is reserved: rename any unrelated variable with that name.

## Opt into task retries

```python
from floe import RetryPolicy

retries = {
    "fetch": RetryPolicy(
        max_attempts=3,
        delay_seconds=5,
        retry_on=(TimeoutError, ConnectionError),
    ),
}
```

This gives `fetch` up to three tries, including the first call. Floe waits five seconds between tries and only retries the named exceptions. Set a positive attempt count, a finite delay of zero or more, and name at least one `Exception` type.

A retry calls the same task again in the same process. It doesn't rerun successful tasks or undo writes. Retry only work that's safe to repeat. If the task runs out of tries or raises a different error, the run stops. A service timeout covers all tries and waits. Direct runs have no overall timeout.

See the [retry example](../examples/retries.py) and [attempt history](history.md#attempt-history-and-json-compatibility).

## Run independent tasks in parallel

```python
from floe import parallel


@parallel
def fetch():
    return 42
```

Register marked functions in `tasks` as usual, then install with `floe install hello.py --workers 2`. Only marked tasks can overlap, and only after their prerequisites finish. Unmarked tasks run alone. Direct `floe run` and `floe.run()` stay sequential. Use `--workers 1` to turn parallel execution off. Parallel mode can't be used with retries.

Parallel workers require a delegated user systemd service, cgroup v2 with `cgroup.kill`, and Linux/Python support for pidfds and memfd. Each worker loads the file again, so keep the code stable during a run. Use results or files to share data, not Python globals. Workers are not a sandbox.

To stop a parallel run, use `floe cancel hello`. To stop only the active `fetch` task, use `floe cancel hello --task fetch`. Tasks that need a cancelled task are skipped; unrelated tasks can continue after a task-only cancel. An ordinary failure stops new tasks from starting but lets active peers finish. A cancel command confirms the request, not the outcome. Check `floe runs hello` or `floe logs hello` for the result.

## Write workflows that run unattended

- Make tasks safe to repeat. A new scheduled run starts from the beginning, even after a failure.
- Give network calls and subprocesses their own timeouts.
- Save durable results to files. To avoid a half-written state file, write a temporary file in the same directory and replace the old file with `os.replace`.
- Use `FLOE_RUN_ID` to link a file or record to the run that produced it.

See the [checksums example](../examples/checksums/) for file-based state or [Feedwatch](../examples/feedwatch/README.md) for a workflow that calls an agent.
