# Working on Floe

"Build the happy path. Anything beyond the requested behavior is a proposal, not part of this
 change." -- Ian Philpot

## Code readability

- Put the main steps in the order they happen. A reader should be able to
  follow the normal path without jumping between helpers.
- Keep checks, limits, and cleanup close to the actions they protect. A
  clear happy path still handles failure; don't hide or remove safeguards
  to make the code shorter.
- Extract a helper when its name explains a real step or gives a tricky
  detail a clear home. Don't add a layer just to shorten a function or file.
  Keep existing boundaries between logic, I/O, and presentation.

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
  separate.

## Releases

When asked to prepare or publish a release, follow [Releases](docs/releases.md)
for versioning, tags, validation, and maintainer approval.

For packaging or service-command changes, build a wheel and validate a clean
installation outside the checkout, including the console entry point and
exact generated interpreter command.

## Testing and validation

```bash
uv sync
uv run pytest -q
uv run pytest -q -m live  # needs the real user systemd session and journald
uv run python examples/deploy_demo.py  # validates/plans; no writes or manager calls
git diff --check
```

## Documentation expectations

Keep the README, docs/, examples, help text, and public contracts aligned.
