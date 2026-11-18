# Quickstart

This guide takes one workflow from a first run to scheduled runs, and then
removes it again. It takes about five minutes, plus up to two minutes of
waiting for a timer. Check the [requirements](../README.md#requirements) first.

## 1. Install Floe

```bash
git clone https://github.com/ipdelete/floe.git ~/src/floe
cd ~/src/floe
uv sync
source .venv/bin/activate   # or prefix floe commands with `uv run`
floe --help
```

Installed workflows run Floe from this checkout. If you move it, run
`floe install` again for each workflow.

## 2. Write a workflow

A workflow is a Python file that defines its name, its tasks, and the order
between them. Create `~/flows/hello/hello.py`:

```python
import sys
from datetime import datetime
from pathlib import Path

WORKFLOW = "hello"
SCHEDULE = ["*-*-* 09:00:00"]  # daily at 09:00, see systemd.time(7)

OUT = Path(__file__).resolve().parent / "greetings.txt"


def greet():
    print("saying hello", file=sys.stderr)


def record():
    with OUT.open("a") as file:
        file.write(f"{datetime.now().isoformat(timespec='seconds')} hello\n")
    print(f"wrote {OUT}", file=sys.stderr)


tasks = {"greet": greet, "record": record}
dag = {"greet": set(), "record": {"greet"}}
```

`dag` lists each task's prerequisites: `record` runs after `greet`.

## 3. Run it directly

```bash
floe run ~/flows/hello/hello.py
```

Floe's events are JSON lines on stdout; what tasks print goes to stderr.
Abbreviated:

```
{"schema": 1, "event": "run_started", "workflow": "hello", "run_id": "5dc249d0…", …}
{"schema": 1, "event": "task_started", …, "task": "greet"}
saying hello
{"schema": 1, "event": "task_completed", …, "task": "greet", "status": "succeeded"}
{"schema": 1, "event": "task_started", …, "task": "record"}
wrote /home/you/flows/hello/greetings.txt
{"schema": 1, "event": "task_completed", …, "task": "record", "status": "succeeded"}
{"schema": 1, "event": "run_completed", …, "status": "succeeded"}
```

Add `> /dev/null` to see only the task output. A failing task stops the run
and makes `floe run` exit with status 1.

## 4. Install it

```bash
floe install --dry-run ~/flows/hello/hello.py   # show the unit files; writes nothing
floe install ~/flows/hello/hello.py
floe list
```

```
wrote: hello.service, hello.timer; hello.timer is enabled
NAME   SCHEDULE        TIMER   NEXT                 LAST RUN  SCRIPT
hello  *-*-* 09:00:00  active  2026-11-18 09:00:00  -         ~/flows/hello/hello.py
```

Floe wrote a systemd service that runs the workflow and a timer that starts it
on schedule. From now on you can use the name `hello` instead of the path.

## 5. Run it under systemd and read its history

```bash
floe start hello    # run now and wait for it
floe runs hello
floe logs hello
```

```
hello.service finished successfully
RUN       STARTED              STATUS     DURATION  DETAIL
3821d124  2026-11-17 13:51:43  succeeded  0.0s
run 3821d124be5e4617b87ec35fcc3bbd03  succeeded  started 2026-11-17 13:51:43  duration 0.0s
13:51:43.084  systemd: Starting hello.service - Floe workflow hello...
13:51:43.154  ▶ run started
13:51:43.154  ▶ greet started
13:51:43.155  [greet] saying hello
13:51:43.155  ■ greet succeeded
13:51:43.155  ▶ record started
13:51:43.155  [record] wrote /home/you/flows/hello/greetings.txt
13:51:43.155  ■ record succeeded
13:51:43.155  ■ run succeeded
13:51:43.166  systemd: Finished hello.service - Floe workflow hello.
```

Runs under systemd, whether started by `floe start` or by the timer, are
recorded in journald; `floe runs` and `floe logs` read them from there. Direct
`floe run` invocations are not recorded.

## 6. Watch the timer start a run

Rather than waiting until 09:00, add a one-off time two minutes from now:

```bash
date -d '+2 min' '+%F %H:%M:00'    # for example 2026-11-17 13:53:00
```

```python
SCHEDULE = ["*-*-* 09:00:00", "2026-11-17 13:53:00"]
```

Install again and wait. The run can start up to a minute after the scheduled
time:

```bash
floe install hello
floe runs hello     # after the scheduled time
```

```
RUN       STARTED              STATUS     DURATION  DETAIL
f3b35dc6  2026-11-17 13:53:01  succeeded  0.0s
3821d124  2026-11-17 13:51:43  succeeded  0.0s
```

Remove the one-off entry and run `floe install hello` again. Timers start runs
only while you are logged in, unless systemd user lingering is enabled
(`loginctl enable-linger`).

## 7. Remove it

```bash
floe uninstall hello
```

```
removed: hello.timer, hello.service
```

This disables the timer and deletes both unit files. Your workflow file and
`greetings.txt` stay where they are, and journald keeps the history:
`floe runs hello` still shows it, with a note that `hello` is not installed.

## Next steps

- [Workflows](workflows.md): the definition rules, and patterns for workflows
  that run unattended, with optional bounded task retries.
- [The service environment](deployment.md#the-service-environment): read this
  if a workflow works with `floe run` but fails under systemd.
- [History](history.md): run statuses and what `floe logs` shows.
- `examples/checksums/` is a workflow that keeps state between runs, and
  [`examples/feedwatch/`](../examples/feedwatch/README.md) one that lets an
  agent make decisions.
