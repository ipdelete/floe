# Architecture

Floe is a library with user interfaces on top. The `floe` command is one UI; a
web UI is planned and will use the same library.

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
```

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
