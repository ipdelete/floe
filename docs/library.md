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
