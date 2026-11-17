# Development

```bash
uv run pytest -q
uv run python examples/deploy_demo.py  # validate calendars and plan deployment without contacting the service manager
```

Normal tests use temporary directories and mock the service manager. History
tests use journal records captured from real systemd (`tests/fixtures`). When
available, `systemd-analyze verify` also checks generated units without installing
them. A test runs the exact service command with the system Python and no
virtual environment, and `tests/test_examples.py` keeps the examples working.

To test against your real user systemd session and journald:

```bash
uv run pytest -q -m systemd_e2e --systemd-e2e
```

This is opt-in and takes about 20 seconds. Each test generates its own workflow,
named `floe-e2e-<random>`, in a temporary directory, deploys it with the real
`floe` command, and uninstalls it afterward, verifying that no unit files,
enable links, loaded units, or timer stamps remain. The tests cover deployment
and idempotent reinstall, task output attribution, history and logs of successful, failed, invalid,
timed-out, running, and stopped runs, overlapping starts, schedule drift,
missing definition files, interrupted uninstalls, foreign and masked units, and
the timer starting runs. They execute only their own generated tasks and touch
no other units. Leftover `floe-e2e-*` units from an interrupted session (for
example after SIGKILL) are removed when the tests start, and can be removed by
hand with `floe uninstall <name>`.
