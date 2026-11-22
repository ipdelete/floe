# Floe

Floe runs Python tasks in the order you set. Use it to run a workflow now or on a schedule. On Linux, systemd starts scheduled runs and journald keeps their logs.

**[Start here: run and schedule your first workflow](docs/quickstart.md).**

## Requirements

- Linux and Python 3.14 or newer
- [uv](https://docs.astral.sh/uv/) to install Floe from this repo
- For scheduled runs and history: a user systemd session and the `systemctl`, `systemd-analyze`, and `journalctl` commands

Tenacity is Floe's only third-party runtime dependency. Floe uses it for retries and installs it automatically.

## Docs

- [Write a workflow](docs/workflows.md)
- [Install and schedule it](docs/deployment.md)
- [Read run history and logs](docs/history.md)
- [Open the local dashboard](docs/web.md)
- [Use the Python library](docs/library.md)
- [Install a release wheel or publish a release](docs/releases.md)

Example maintenance workflow: [update and verify pr-agent daily](examples/pr_agent/README.md).

Contributing? See [testing guide](tests/AGENTS.md) and [CI](.github/workflows/ci.yml).
