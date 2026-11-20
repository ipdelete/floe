# Testing Floe

## Method

Write tests from the outside in:

1. Start from a user claim and write one live e2e test for it.
2. Trace the path that test takes and write contract tests for the seams it
   crosses, with real components on both sides.
3. For each component behind a contract, write unit tests for its logic.
   Fake only external boundaries, copying the shapes they really produce.

What a layer teaches you can change the others. A new claim starts a new
pass.

Browser tests live in `tests/web/`. They follow the same method: a real browser
against the real dashboard, systemd, and journald is a live e2e, not a separate
layer. Use role/name selectors and Playwright's waiting assertions rather than
fixed browser sleeps. Keep setup and cleanup close to the resources they own.

## Local checks

Run commands from the repository root:

```bash
uv sync
uv run pytest -q
uv run pytest -q -m 'live and not web'  # needs the real user systemd session and journald
uv run python examples/deploy_demo.py  # validates/plans; no writes or manager calls
git diff --check
```

The default test run excludes `live` and `web` tests. Live tests need a real user
systemd session and journald; see the requirements in [README.md](../README.md).

## Browser setup and execution

Install Playwright's Chromium browser once after syncing dependencies:

```bash
uv run playwright install chromium
```

If Chromium reports missing OS libraries, install them with the following command
(requires administrator privileges):

```bash
uv run playwright install-deps chromium
```

Run the browser tests with the real user systemd session and journald:

```bash
uv run pytest -q -m web --browser chromium --tracing retain-on-failure --screenshot only-on-failure
```

Failure screenshots and traces are written to `test-results/` (ignored by git).
Open a trace with `uv run playwright show-trace <path-to-trace.zip>`.
