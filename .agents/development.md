# Development

```bash
uv run pytest -q
uv run python examples/deploy_demo.py  # validate calendars and plan deployment without contacting the service manager
```

Normal tests use temporary directories and mock the service manager. History
tests use journal records captured from real systemd (`tests/fixtures`). When
available, `systemd-analyze verify` also checks generated units without installing
them. Tests run the exact service command with the recorded installing Python
environment and no shell activation; `tests/test_examples.py` keeps examples
working. Retry tests use real Tenacity decisions with controlled waits rather
than wall-clock sleeps.

Web tests use temporary unit files, a fake journal and service manager, and a
real ephemeral loopback HTTP server. They check navigation, output escaping,
read-only behavior, bounded reads, caching, and HTTP restrictions. They do not
contact the real user systemd session.

The packaging test uses `uv` to build a wheel offline and install it without
registry access into a temporary virtual environment outside the checkout,
including cached Tenacity. It checks the console entry point, isolated imports,
bundled icon resources, a retry workflow through the exact generated service
command, deployment planning, and offline unit verification. It does not contact a package registry or service manager.

## Engineering methods

For session initialization and agent-document authoring, see the
[project skills](README.md).

Use the repository's [Spike, Design, and Implement skills](engineering/README.md)
to investigate uncertainty, make evidence-based decisions, and build against the
resulting intent. They are complementary methods, not required stages for every
change. Keep useful findings and consequential decisions under
`.agents/engineering/<effort>/`; prototype code stays in each effort's
`experiments/` directory on spike branches only. Product documentation stays in
`docs/`.

For disposable experiments, use the
[spike branch and evidence-handoff workflow](engineering/README.md#spike-branches-and-handoff):
keep competing approaches independent and carry findings, rather than prototype
implementations, into design. A spike branch is sufficient; a discussion PR is
optional and not intended for merge.

Work intended for `master`, including durable evidence/design documentation,
follows the [PR workflow](../AGENTS.md#development-and-pull-request-workflow):
focused branch and PR, PR-Agent review, valid findings addressed, CI passing on
the latest commit, then authorized squash merge and safe branch cleanup. Skills
and passing CI never authorize real-systemd tests or replace human architectural
approval.

The [feature intake service](../tools/intake/README.md) is a standalone local
maintainer tool. It starts a Pi Durable session from an HTTP request and publishes
a feature issue. It does not install with Floe or start later workflow stages.

## Continuous integration

[CI](../.github/workflows/ci.yml) runs on PRs and pushes to `master`. It uses
Ubuntu 26.04, checks that systemd executables exist, uses Python 3.14 environments,
installs locked development dependencies, and runs the normal tests with
`systemd_e2e` explicitly excluded. It also checks committed whitespace, validates
deployment offline, builds a wheel through the source distribution, and tests
that wheel's commands/resources in a clean environment outside the checkout.
No installed workflow is started and no systemd session is modified.

Distributions and checksums are saved as Actions artifacts for 30 days. The
[Release workflow](../.github/workflows/release.yml) reuses CI for a version tag
and publishes the exact saved files after approval, not rebuilt distributions.
See [Releases](../docs/releases.md) for setup, version policy, and maintainer steps.
The small inline tag/version/changelog check has regression tests in
`tests/test_release_version.py`; there is no custom publisher or CI image.

## Opt-in real-session tests

With explicit approval, the **live managed-worker integration test** can be
selected on its own:

```bash
uv run pytest -q tests/test_managed_systemd_e2e.py --systemd-e2e
```

This test creates one transient, runtime-limited `floe-e2e-managed-*.service`
with `Delegate=yes` in the user manager (Linux cgroup v2; no sudo or persistent
unit installation). It invokes the private managed observer, not `floe run`.
It checks that a detached child with no output pipe holds up a bound dependent,
then verifies both natural exit and forced drain-timeout cleanup. The probe
collects the child's actual exit status separately. The test checks unit
collection and cgroup removal. It does **not** test supervisor death or replace
installed-service lifecycle tests. Never run this or the full real-session suite
without explicit authorization for the target session.

`tests/test_parallel_systemd_e2e.py` is the product-level counterpart: it uses
`floe install`, `start`, `cancel`, `logs` and `uninstall`, without calling the
worker or scheduler under test. Select it explicitly with `--systemd-e2e` only
with authorization to install the generated services in the target session.

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
