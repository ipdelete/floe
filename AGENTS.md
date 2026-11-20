# Working on Floe

"Build the happy path. Anything beyond the requested behavior is a proposal, not part of this
 change." -- Ian Philpot

## Suggestions and rules

- Suggestions are defaults, followed with judgment. They live in AGENTS.md and get broken when there's a reason. Most
  guidance belongs here: readable code, module separation, keeping the library free of UI work, test style.
- Rules are rare: only things where breaking them actually breaks something, or where you've said "never". Those get
  enforced mechanically by a test, a git hook or a pi hook, not by prose.
- Tests check behavior. The runner runs them, and that's the check. A test doesn't need its own validation ceremony.

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
  environment, including Floe and its dependencies. Planning requires `systemd-analyze`.
- Use the project's dependencies wherever they help.
  Document external executables separately and report missing tools clearly.
- Business logic goes in the library, `src/floe/`; the UIs hold UI logic, not
  business logic. The library returns structured data or raises `FloeError`s
  rather than printing, prompting, exiting, or parsing arguments, and it doesn't
  import a UI.
- `src/floe_cli/` handles arguments, presentation, stdout/stderr routing, and exit
  codes. Interfaces and examples use what `floe.__all__` exports.
- Rendering, file operations, service-manager calls, and journal access each have
  their own module. Keep it that way while it keeps the code readable; don't add
  layers just to enforce it.

## Releases

When asked to prepare or publish a release, follow [Releases](docs/releases.md)
for versioning, tags, validation, and maintainer approval.

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
