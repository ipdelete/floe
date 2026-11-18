# Changelog

Release notes use SemVer headings, without the `v` tag prefix. Unreleased work
moves into a dated version section when preparing a release PR.

## [Unreleased]

## [0.1.0] - 2026-11-18

Initial release.

### Added

- Sequential, fail-fast Python workflows managed by systemd user services and
  timers, with journald execution history. No retries or parallel task execution.
- Ownership-aware installation and recoverable cleanup, with offline deployment
  planning and systemd calendar validation.
- CLI commands for running, installing, inspecting, and removing workflows, with
  structured JSON/events and separate diagnostics.
- Read-only local dashboard with workflow/run views, bounded logs, task navigation,
  explicit partial/unknown states, and the original two-piece Floe mark.
- GitHub Actions validation and approved, SemVer-tagged GitHub Releases containing
  a wheel, source distribution, and SHA-256 checksums. No PyPI publication.

### Requirements and compatibility

- Linux, a systemd user session, Python 3.14+, and `uv` for installation. Deployed
  services also require `/usr/bin/python3` 3.14+; systemd command-line tools are
  required for scheduling, history, and deployment validation.
- Workflow definitions are trusted executable Python, not sandboxed data.
- The dashboard is loopback-only and unauthenticated, not intended for public
  hosting. Other local users able to connect may read it; logs may contain secrets.
- Pre-1.0 compatibility policy applies: minor releases may change public contracts.
  Installing an updated package does not reinstall workflow units or restart a
  running dashboard. See the release and deployment guides for manual steps.
