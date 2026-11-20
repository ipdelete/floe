# Read-only local dashboard

```bash
floe web                 # http://127.0.0.1:8080/
floe web --port 8123      # use a different local port
```

Open the printed address in your browser. Pick a workflow to see its schedule and recent runs. Open a run to see its tasks, errors, and logs. Expand **Attempt history** to see retry details. Use **Refresh** to update the page and Ctrl-C in the terminal to stop the server.

The dashboard shows times in the server's local time zone. It looks for run activity in the last seven days, shows up to 20 runs per workflow, and reads only the newest 1,000 journal records per run. It labels partial history and may shorten long messages. For full logs still in the journal, use `floe logs NAME RUN_ID`.

## Browser tests

For the live Playwright test and browser setup, see
[Testing Floe](../tests/AGENTS.md#browser-setup-and-execution).

## Read-only and local

The dashboard reads installed metadata, systemd state, and journal records. It doesn't load Python definitions, start tasks, or change units. Use `floe list --check` if you want to check definitions against installed units.

The server listens only on `127.0.0.1` and has no login. Other users or processes on the same machine may be able to read it. Logs can contain secrets: don't expose or forward the port to untrusted clients.
