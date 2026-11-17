# Working on Floe

## Project and architecture

Floe is a minimal local workflow runner: Python functions execute sequentially,
systemd manages scheduling and processes, and journald is the history store.
There are no retries or parallel task execution. Read [README.md](README.md) and
[docs/](docs/) for the supported behavior and public API before changing a
feature.

- Use Python 3.14+ and `uv`. Deployed services also require `/usr/bin/python3`
  to be 3.14+. Deployment planning requires `systemd-analyze`.
- Keep Python runtime dependencies standard-library-only. Document required
  external executables separately and report missing tools clearly.
- `src/floe/` owns logic and data. It must not print, prompt, exit, parse CLI
  arguments, or import a UI. Return structured data or raise structured errors;
  use the `FloeError` hierarchy for intentional library errors.
- `src/floe_cli/` owns arguments, presentation, stdout/stderr routing, and exit
  codes. Interfaces and examples use only exports in `floe.__all__`.
- Keep rendering, file operations, service-manager calls, and journal access
  separate. `tests/test_boundaries.py` enforces architectural constraints.

## Non-negotiable safety rules

- Do not run real-systemd E2E tests or modify the user's systemd session without
  explicit approval. Never operate on unrelated units or clean up arbitrary
  files. Generated E2E workflows use the `floe-e2e-` prefix.
- Workflow Python is trusted executable code, not sandboxed data. Execute it
  only where documented; metadata-only inspection must not load definitions.
- Preserve sibling-import isolation during both loading and task execution.
  Restore `sys.path` and `sys.modules` even after exceptions. Loading/execution
  is serial and must not race with other threads importing sibling modules.
- Treat ownership of files and ownership of loaded units as separate facts.
  An owned file does not authorize stopping a foreign loaded fragment.
- Unknown state is not absent, safe, or successfully cleaned. Report failed
  queries instead of silently turning them into successful cleanup.
- Validate both units and calendar expressions before installation writes.
  Recheck ownership immediately before destructive file operations. Never
  overwrite masks, symlinks, or directories, including with `--adopt`.
- Keep planning separate from applying. Dry runs may inspect and validate, but
  must not write files or contact the service manager. Use systemd's calendar
  parser rather than implementing a competing parser.
- Make cleanup recoverable: attempt independent steps, aggregate failures, and
  retain enough ownership information to retry unfinished work. Do not remove
  the last cleanup marker before timer-state cleanup succeeds.
- Preserve per-file atomic replacement without claiming the unit pair is a
  transaction. Repeated operations must handle partial prior success.
- Keep JSON/event stdout separate from diagnostics. Definition/import output
  belongs on stderr at the CLI boundary, not in machine-readable output.
- Every accepted workflow name must remain addressable. Preserve explicit name
  targeting for file-like names and operation after definition deletion.

## Implementation conventions

- Prefer explicit state and straightforward control flow over fewer lines.
  Use loops for coordinating side effects and comprehensions for transformations.
- Normalize and snapshot inputs before validation; consume iterators only once.
  Avoid reconstructing validated objects unnecessarily. This does not replace
  safety rechecks at filesystem or service-manager boundaries.
- Validate runtime inputs explicitly; annotations and `Literal` are not runtime
  checks. Collect independent definition problems rather than failing at the
  first malformed prerequisite. Do not use assertions for user-input validation.
- Prefer dataclasses to dictionaries with an implicit record schema. Add small
  helpers with clear responsibilities, not generic frameworks for hypothetical
  future features. Remove unreachable behavior and its tests together.
- Reconstruct history by invocation identity, not record adjacency. Delayed or
  interleaved records must not truncate another invocation's history.
- Validate event schemas, event-specific fields, statuses, and timezone-aware
  timestamps before using them as history evidence. Malformed event-shaped JSON
  remains ordinary output. Missing evidence is not proof of success or failure.
- Use subprocess argument lists and systemd-specific escaping, not shell quoting
  as a substitute. Bound operations appropriately; a synchronous workflow start
  may legitimately wait for its configured workflow timeout. Streaming readers
  must terminate and reap producers when callers stop early.
- Keep refactors focused and behavior-preserving unless a contract change is
  intentional and documented. Measure realistic workloads before optimizing.

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
  system-Python command. Run offline unit verification where applicable.
- With explicit approval, run the real-session suite separately:
  `uv run pytest -q -m systemd_e2e --systemd-e2e`.
- Report what ran, what was skipped, and remaining validation gaps. Passing mocks
  or offline unit verification does not establish real-systemd lifecycle behavior.

## Documentation expectations

Keep the README, docs/, examples, help text, and public contracts aligned. Document
prerequisites, execution side effects, JSON/exit-code behavior, recovery steps,
and compatibility limits when they change. Do not broaden guarantees beyond
what implementation and validation support.
