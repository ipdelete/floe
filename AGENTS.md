# Working on Floe

## Project and architecture

Floe is a minimal local workflow runner: Python functions execute sequentially,
systemd manages scheduling and processes, and journald is the history store.
Task retries are bounded and opt-in; task execution is sequential. Read [README.md](README.md) and
[docs/](docs/) for the supported behavior and public API before changing a
feature.

- Use Python 3.14+ and `uv`. Deployed services retain the installing interpreter
  environment, including Floe and Tenacity. Planning requires `systemd-analyze`.
- Tenacity is the sole approved third-party runtime dependency, confined to the
  retry mechanism. Document external executables separately and report missing tools clearly.
- `src/floe/` owns logic and data. It must not print, prompt, exit, parse CLI
  arguments, or import a UI. Return structured data or raise structured errors;
  use the `FloeError` hierarchy for intentional library errors.
- `src/floe_cli/` owns arguments, presentation, stdout/stderr routing, and exit
  codes. Interfaces and examples use only exports in `floe.__all__`.
- Keep rendering, file operations, service-manager calls, and journal access
  separate. `tests/test_boundaries.py` enforces architectural constraints.

## Releases

When asked to prepare or publish a release, follow [Releases](docs/releases.md)
for versioning, tags, validation, and maintainer approval.

## Testing and validation

```bash
uv sync
uv run pytest -q
uv run python examples/deploy_demo.py  # validates/plans; no writes or manager calls
git diff --check
```

- Add a concrete regression test for each bug. Ask what plausible broken
  implementation would still pass; use targeted mutation probes when useful.
- Assert observable behavior, not just execution without exceptions. Filter
  tests need observable filtering; error-isolation tests need multiple workflows;
  streaming tests need controlled producers, timeouts, and reliable cleanup.
- Mock external boundaries, not the logic under review. Keep fakes faithful to
  the properties being tested. Use temporary directories for normal tests.
- Remove redundant tests, but retain useful smoke, architecture, packaging, and
  public-API contracts. An exported API is not dead code for lacking callers.
- For packaging or service-command changes, build a wheel and validate a clean
  installation outside the checkout, including the console entry point and
  exact generated interpreter command. Run offline unit verification where applicable.
- Run the real-session suite separately:
  `uv run pytest -q -m systemd_e2e --systemd-e2e`.
- Report what ran, what was skipped, and remaining validation gaps. Passing mocks
  or offline unit verification does not establish real-systemd lifecycle behavior.

## Documentation expectations

Keep the README, docs/, examples, help text, and public contracts aligned.
