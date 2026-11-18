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

Web tests use temporary unit files, a fake journal and service manager, and a
real ephemeral loopback HTTP server. They check navigation, output escaping,
read-only behavior, bounded reads, caching, and HTTP restrictions. They do not
contact the real user systemd session.

The packaging test uses `uv` to build a wheel offline and install it without
dependencies into a temporary virtual environment outside the checkout. It
checks the console entry point, isolated imports, and bundled icon geometry and
license resources. It does not contact a package registry or service manager.

## Engineering methods

For session initialization and agent-document authoring, see the
[project skills](../.agents/README.md).

Use the repository's [Spike, Design, and Implement skills](engineering/README.md)
to investigate uncertainty, make evidence-based decisions, and build against the
resulting intent. They are complementary methods, not required stages for every
change. Keep useful findings and consequential decisions under
`docs/engineering/<effort>/`; prototypes are disposable by default.

The [PR workflow](../AGENTS.md#development-and-pull-request-workflow) is unchanged:
focused branch and PR, PR-Agent review, valid findings addressed, CI passing on
the latest commit, then authorized squash merge and safe branch cleanup. Skills
and passing CI never authorize real-systemd tests or replace human architectural
approval.

## Continuous integration

[CI](../.github/workflows/ci.yml) runs on PRs and pushes to `master`. It uses
Ubuntu 26.04, checks that `/usr/bin/python3` is 3.14+ and the systemd executables
exist, installs locked development dependencies, and runs the normal tests with
`systemd_e2e` explicitly excluded. It also checks committed whitespace, validates
deployment offline, builds a wheel through the source distribution, and tests
that wheel's commands/resources in a clean environment outside the checkout.
No installed workflow is started and no systemd session is modified.

Distributions and checksums are saved as Actions artifacts for 30 days. The
[Release workflow](../.github/workflows/release.yml) reuses CI for a version tag
and publishes the exact saved files after approval, not rebuilt distributions.
See [Releases](releases.md) for setup, version policy, and the maintainer steps.
The small inline tag/version/changelog check has regression tests in
`tests/test_release_version.py`; there is no custom publisher or CI image.

## Opt-in real-session tests

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
