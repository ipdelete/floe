# Inspect execution history

Every run under systemd (scheduled or `floe start`) is recorded in journald.
Floe reconstructs runs from those records:

```bash
floe runs mydag                       # latest 20 runs, newest first
floe runs mydag --since "24 hours ago" -n 50
floe logs mydag                       # events and output of the latest run
floe logs mydag 4581dde1              # a specific run, by ID or unique prefix
floe runs mydag --json                # also: floe logs mydag --json
```

```
RUN       STARTED              STATUS     DURATION  DETAIL
6904b066  2026-11-16 07:46:18  failed     0.1s      invalid workflow: unknown prerequisites: 'missing'
29b24993  2026-11-16 07:45:25  failed     2.3s      transform: broken input
ca9f4c32  2026-11-16 07:34:38  succeeded  0.0s
```

Runs are ordered by start time (or the earliest available record if the start
was lost). Discovery streams the matching journal history, then fetches complete
records for the selected invocations; delayed records from an older run do not
truncate a newer run. `--since` selects runs with journal activity in that range,
but their displayed history also includes records from before the cutoff.

Only schema-valid events with timezone-aware timestamps and matching workflow
and invocation identities are used to reconstruct runs. Other JSON, including
malformed event-shaped output, remains ordinary output. If a task invokes a
different workflow with `floe run`, that child's events remain output of the
parent task, even when the child inherits the same systemd invocation ID.

A run's ID is its systemd invocation ID. Its status is:

- `succeeded` / `failed`: from the run's `run_completed` event. A run that
  failed before starting (for example an invalid definition) is `failed` with
  no tasks, and its error output as the error.
- `running`: no `run_completed` yet and the service is still active.
- `incomplete`: the run started but ended without `run_completed` (killed,
  timed out, crashed), with systemd's result (such as `timeout`) when known.

`floe logs` shows systemd's messages, Floe's events, and task output in order,
with output attributed to the task that was running. Each line's time is when
journald received it; in `--json`, an event line's `event` also keeps the
event's own timestamp, which run start, finish, and durations are based on.
Runs of `floe run` outside systemd are not in the journal and do not appear.

## Attempt history and JSON compatibility

Workflows without configured retries (including `retries = {}`) keep the schema-1 lifecycle sequence.
Workflows with retry policies emit schema 2: the existing run/task events plus
`attempt_started`, `attempt_completed`, and `retry_scheduled`. Only configured
tasks have attempt events. Readers accept both schemas; old history has an empty
attempt list, not a synthesized successful or failed attempt.

Each attempt event carries `task`, one-based `attempt`, and `max_attempts`.
Attempt completions carry `status` (`succeeded` or `failed`) and an optional error.
`retry_scheduled` carries the just-failed attempt number and `delay_seconds`;
it is evidence of an intended wait, not proof another attempt started. Final
retry-configured task failures carry `failure_reason`: `exhausted` or
`non_retryable`. Interruption with no completion remains `incomplete`, not
exhausted. Attempt identity is `(run_id, task, attempt)`.

Run JSON adds task `attempts`, `max_attempts`, `retry_at`, and `failure_reason`.
Each attempt has `number`, `status`, `started`, `finished`, and `error`. Missing
starts remain null, including after journal truncation. Log JSON adds `attempt`
(null when unknown/not executing an attempt). Failed attempts and their output
are retained even when the final task/run succeeds. During waits, output has no
attempt attribution. As before, child output delayed beyond its task/attempt
boundary cannot reliably be assigned to its original writer.

CLI summaries show current attempts, scheduled waits, exhaustion, and success
after retries. Dashboard run pages show the current state and an expandable
attempt history. Logs label each attempt's events and output.

Consumers must accept additive JSON fields, understand schema 2 and its new
event types, and avoid treating each failed attempt as a final task failure.
Exact-key/schema-1-only consumers require updates. Schema 1 reads remain supported;
malformed attempt events remain ordinary output. `EVENT_SCHEMA` is now 2 (latest
supported schema), although unconfigured execution intentionally still emits 1.

Journald is the only execution-history store. Retention depends on the machine's
journal configuration and storage limits; logs may be rotated or lost across
reboots without persistent journaling. The raw records are also available with
`journalctl --user -u mydag.service` (add `-o json` for structured records, `-f`
to follow).

The [read-only dashboard](web.md), started with `floe web`, shows the same
history with a seven-day discovery window and a newest-1,000-record limit per
selected run. Partial history is labeled. CLI commands do not apply that
record limit; their JSON run objects include `truncated: false`.
