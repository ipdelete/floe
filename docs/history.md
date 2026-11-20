# Inspect execution history

```bash
floe runs mydag                       # latest 20 runs
floe logs mydag                       # latest run's events and output
floe logs mydag 4581dde1              # a run ID or unique prefix
floe runs mydag --since "24 hours ago" -n 50
```

Only runs through systemd appear here. `floe run` outside systemd doesn't write history. Logs stay available after uninstall, for as long as journald keeps them. For raw records, use `journalctl --user -u mydag.service`.

| Status | Meaning |
| --- | --- |
| `succeeded` / `failed` | The run finished with a known outcome. A definition error can fail a run before tasks start. |
| `running` | The run hasn't finished and the service is still active. |
| `cancelled` | Floe confirmed a cancellation and recorded no failure. |
| `incomplete` | The run ended without a final event. Check the systemd result for clues such as a timeout. |

`--since` finds runs with journal activity in that window. Floe then reads each selected run's records, including any from before the cutoff. The [dashboard](web.md) reads fewer records per run than the CLI and labels partial history.

## Attempt history and JSON compatibility

Add `--json` to `floe runs` or `floe logs` for scripts. Allow extra fields and handle all supported event schemas: 1 for ordinary sequential runs, 2 for sequential runs with retry policies, and 3 for installed parallel runs. JSON that isn't a valid event for the workflow and invocation stays ordinary output.

With retries, `attempt_started` and `attempt_completed` use one-based attempt numbers. `retry_scheduled` means Floe planned to wait, not that another attempt started. A failed attempt can still end in a successful task. Final retry failure is `exhausted` or `non_retryable`; an interrupted attempt isn't marked exhausted.

Run JSON includes task `attempts`, `max_attempts`, `retry_at`, and `failure_reason`. Old history has no attempt records. Each attempt has `number`, `status`, `started`, `finished`, and `error`; missing times are null. Log JSON includes `attempt` when known (null otherwise). An event's timestamp and the time journald received it can differ.

In parallel runs, worker output has `kind: "output"` and an explicit `task` field. `cancelled` means a task was stopped; `skipped` means it didn't start. Task return values aren't logged.
