# Python library

The `floe` CLI and dashboard use the same public Python API. Use names exported by `floe` (`floe.__all__`). The library returns structured data or raises a `FloeError`; the interfaces handle text and exit codes.

## Run a workflow

```python
import floe

workflow = floe.load("examples/workflow.py")
succeeded = floe.run(workflow, on_event=lambda event: print(event.to_dict()))
```

Loading runs the file's top-level Python code, so only load files you trust. Loading and execution temporarily change Python's import state. Run them serially, not alongside threads that import same-named helper modules.

## Preview installation

```python
store = floe.UnitStore(floe.user_unit_dir())
deployment = floe.Deployment.for_workflow(workflow)
plan = floe.plan_install(deployment, store)
for change in plan.changes:
    print(change.name, change.action, change.diff())
```

Planning checks the installation without writing units or contacting the service manager. `Deployment.python` defaults to the current interpreter's absolute path, preserving virtualenv symlinks. `Deployment.for_workflow(workflow, workers=2)` opts an installed service into parallel mode; `floe.run()` remains sequential. See [deployment](deployment.md) for environment requirements.

## Read history

```python
for run in floe.list_runs("mydag", floe.Journal(), floe.Systemd(), limit=5):
    print(run.run_id, run.status, run.duration)
```

History and inventory times are timezone-aware UTC values, or `None` when unknown. `list_runs()` and `run_logs()` accept `max_records` to read only the newest records per selected run. `Run.truncated` indicates omitted records. `overview()` accepts `since` and `max_records` for its latest-run queries. It reports timer and history query failures separately. Use `WorkflowSummary.timer_state` to distinguish unknown state (`None` or a missing property) from inactive or disabled.

## Define a workflow in Python

`Workflow` snapshots its task metadata, including `inputs`, `retries`, and marked `parallel_tasks`. Its `dag` includes prerequisites from both `dag` and `inputs`. `to_dict()` includes configured bindings and marked task names, not return payloads. See [result bindings](workflows.md#pass-small-results-to-downstream-tasks), [retry policies](workflows.md#opt-into-task-retries), and [history schemas](history.md#attempt-history-and-json-compatibility).

## Source layout

- `src/floe/`: workflow logic, deployment, and history. No user-facing I/O.
- `src/floe_cli/`: command arguments, output, and exit codes.
- `src/floe_web/`: local HTTP server and read-only views.
