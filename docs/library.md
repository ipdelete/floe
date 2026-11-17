# Architecture

Floe is a library with user interfaces on top. The `floe` command and the
read-only `floe web` dashboard use the same public library.

```
src/floe/        library: all logic, no user I/O
  model.py       Workflow, Event, InstallPlan, UnitChange, UnitState; FloeError hierarchy
  loader.py      load(path) -> Workflow
  runner.py      run(workflow, on_event) -> bool
  deploy.py      Deployment, plan_install/apply/install, uninstall, start, status
  history.py     list_runs, get_run, run_logs -> Run, TaskRun, RunLogs, LogLine
  inventory.py   list_installed, find_installed, check_installed, overview -> InstalledWorkflow, WorkflowSummary
  render.py      unit file text
  store.py       ownership-protected unit files
  systemd.py     thin systemctl wrapper
  journal.py     thin journalctl wrapper
src/floe_cli/    the `floe` command: arguments in, text and exit codes out
src/floe_web/    localhost HTTP routing, escaped server-rendered HTML, small stylesheet
```

The web UI never loads workflow definitions and has no write routes. See
[Dashboard](web.md) for its query limits and local-access constraints.

`WorkflowSummary.timer_state` contains the queried timer `UnitState`, or `None`
when the timer query failed. Its properties preserve the actual activity and
unit-file states, including runtime-only enablement; missing properties remain
unknown. The additive JSON `timer_state` field is that state's `to_dict()` or
`null`. A populated value has the shape
`{"name": "mydag.timer", "properties": {"ActiveState": "active", "UnitFileState": "enabled"}}`;
property keys and values are strings, and missing properties are not filled in.
The existing `active` and `enabled` convenience booleans and positional constructor
arguments are unchanged. Consumers enforcing an exact set of JSON keys must allow
the new field. Use the raw state when distinguishing unknown from inactive/disabled matters.

History and inventory query APIs return timezone-aware UTC `datetime` objects
for recorded times; unavailable times are `None`. Lifecycle events without a UTC
offset are not accepted as history evidence. The web renderer converts these
aware values to server-local time rather than guessing a timezone for naive data.

`list_runs(..., max_records=N)` and `run_logs(..., max_records=N)` optionally
read only the newest N records per selected invocation (plus one to detect
truncation). The returned `Run.truncated` flag, also present in `to_dict()`,
marks omitted earlier records. Missing starts in a truncated run have no start
time or duration; task history can be partial. Without `max_records`, behavior
is unchanged. With bounded `list_runs`, candidates are selected and ordered
using evidence in the discovery window before their limited records are
fetched. `overview(..., since=..., max_records=...)` forwards these limits for
each workflow's latest run. `Journal.records(..., limit=N)` applies a newest-
record limit in journalctl itself, preserving the requested output order.

The rules, enforced by `tests/test_boundaries.py`:

- The library never prints, prompts, exits, parses arguments, or imports a UI.
  It returns data types from `floe.model` (each has `to_dict()` for JSON) or
  raises `FloeError` subclasses with structured fields.
- UIs use only the public API exported by `floe` (`floe.__all__`).
- Everything is standard library only.

Workflow loading and task execution temporarily change Python's process-global
import state. Use them serially, not concurrently with other threads importing
same-named sibling modules.

Use the library directly:

```python
import floe

workflow = floe.load("examples/workflow.py")
floe.run(workflow, on_event=lambda event: print(event.to_dict()))

plan = floe.plan_install(floe.Deployment.for_workflow(workflow), floe.UnitStore(floe.user_unit_dir()))
for change in plan.changes:
    print(change.name, change.action, change.diff())

for past in floe.list_runs("mydag", floe.Journal(), floe.Systemd(), limit=5):
    print(past.run_id, past.status, past.duration, past.failed_task)
```
