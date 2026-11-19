# Releases

Floe ships through [GitHub Releases](https://github.com/ipdelete/floe/releases),
not PyPI. A release contains a Python wheel, source distribution, and
`SHA256SUMS`; its notes come from `CHANGELOG.md`. Checksums cover the two
distributions. The saved Actions artifact also contains the notes, and its digest
is verified when downloaded for publication. Publishing does not install,
restart, or change anything on a user's machine or in a systemd session.

## Version policy

Before 1.0, use patch versions for compatible fixes and minor versions for new
features or breaking changes. Call out breaking changes and migration steps in
the notes. After 1.0, use normal SemVer: major for breaking changes, minor for
additive features, patch for compatible fixes. Public contracts include Python
exports, CLI arguments/exit codes, JSON/event schemas, and workflow execution and
deployment behavior, not exact UI appearance.

Supported tags and package versions:

| Git tag | `pyproject.toml` / `uv.lock` version | Changelog heading |
| --- | --- | --- |
| `v0.2.0` | `0.2.0` | `## [0.2.0] - YYYY-MM-DD` |
| `v0.2.0-alpha.1` | `0.2.0a1` | `## [0.2.0-alpha.1] - YYYY-MM-DD` |
| `v0.2.0-beta.1` | `0.2.0b1` | `## [0.2.0-beta.1] - YYYY-MM-DD` |
| `v0.2.0-rc.1` | `0.2.0rc1` | `## [0.2.0-rc.1] - YYYY-MM-DD` |

Leading zeroes, arbitrary prerelease identifiers, and build/local metadata are
not supported. Alpha/beta/RC releases are marked as GitHub prereleases.

## One-time repository setup

Before creating the first release tag:

1. In **Settings → Environments**, create an environment named **`release`**.
   Add at least one required reviewer. The workflow checks this configuration
   and fails closed if the environment/reviewer is missing or cannot be queried.
   A solo maintainer must be able to approve their own release run.
2. Restrict that environment to the intended `v*` tags. Add a tag ruleset to
   restrict creation to release maintainers and prevent moving/deleting release
   tags. Protect `master` with review and the CI check as appropriate.
3. Enable **immutable releases** in repository settings if available. The action
   uploads assets to a draft before publishing, so locking happens after upload.
4. Allow the pinned `actions/*`, `astral-sh/setup-uv`, and
   `softprops/action-gh-release` actions in the repository's Actions policy.

Only the publish job has `contents: write`; normal CI is read-only. Publishing
uses the built-in `GITHUB_TOKEN`, with no PAT, registry token, PyPI account, or
OIDC publisher configuration. This setup must be done by a repository maintainer;
committing workflow YAML does not create approval rules or tag protection.

## Prepare a release

1. Start a release branch from current `master`, and choose the version. For
   example (this guide does not itself release `0.2.0`):

   ```bash
   git switch master
   git pull --ff-only
   git switch -c release/0.2.0
   uv version 0.2.0 --no-sync  # updates pyproject.toml and re-locks the project
   uv lock --check
   uv sync --locked
   ```

2. Move the relevant **Unreleased** entries into exactly one nonempty, dated
   `## [0.2.0] - YYYY-MM-DD` section. Leave an Unreleased section for later work.
   Include prerequisites, compatibility changes, and reinstall/recovery steps
   when relevant; do not imply that all unit upgrades are automatic.
3. Run the normal validation from [Development](../.agents/development.md), commit the
   version/lockfile/changelog changes, and open a release PR. Wait for CI and
   review, then merge it.
4. Tag the exact merged commit, not the pre-merge branch commit:

   ```bash
   git switch master
   git pull --ff-only
   git tag -a v0.2.0 -m 'Floe 0.2.0'
   git push origin v0.2.0
   ```

5. Watch **Actions → Release**. It checks tag/package/lockfile/notes agreement,
   requires the tagged commit to be on `master`, and runs the same CI validation.
   Review the commit, notes, results, and saved distribution artifact; then
   approve the **`release`** environment deployment.
6. The publish job downloads that exact artifact by ID (no rebuild), verifies
   checksums and the remote tag, and refuses an existing release or draft.
   `softprops/action-gh-release` creates a draft and uploads the wheel, source
   distribution, and checksum file with overwriting disabled. The final step
   checks the tag again and publishes the completed draft. Stable Latest
   selection uses GitHub's legacy ordering; prereleases are not Latest releases.
7. Check the release page, files, notes, and prerelease flag. Never move a
   published tag, replace its assets, or reuse its version for different bytes.

No release is created on an ordinary merge to `master`. There is no automatic
version bump, release PR, deployment, workflow start, or real-systemd E2E run.

## Recover a failed release

Build artifacts are retained for 30 days. Keep the original artifact: it is the
source of truth for the bytes that passed validation.

- **No release/draft exists:** fix configuration or transient failures and use
  **Re-run failed jobs**. If validation succeeded, this reuses that job's artifact;
  do not rebuild by rerunning all jobs or move the tag. If validation itself
  failed before producing an approved artifact, rerunning it builds and validates
  the distributions normally; there are no authorized publication bytes to reuse yet.
- **A draft exists:** the automatic job deliberately stops rather than adopting
  or modifying it. Inspect the failed run and that exact tag's draft. Download
  the original Actions artifact with `gh run download RUN_ID --repo ipdelete/floe
  --name ARTIFACT_NAME --dir recovery`; verify its `SHA256SUMS`. Separately
  download the draft assets and compare their bytes against that artifact.
  Stop if anything differs or is unexpected. Upload only missing files with
  `gh release upload TAG FILE --repo ipdelete/floe` (never `--clobber`). Download
  the completed asset set to a fresh directory and check it against the original
  artifact's checksum file. Confirm the tag still points to the validated commit
  and the draft has the right notes/prerelease flag, then publish in GitHub or
  with `gh release edit TAG --repo ipdelete/floe --draft=false`.
- **Already published:** verify the release. Do not retry uploads or delete it.
  If it is wrong, prepare a new version with a correction in the notes.
- **Original artifacts expired/unavailable:** stop rather than guessing or
  rebuilding changed bytes under an existing version. Use a new version when
  the original validated bytes cannot be recovered.

Publication is not a transaction across uploads and metadata. Failures can leave
a draft; they must not be interpreted as successful publication. Do not manually
create or edit a release/draft for the same tag while publication is running:
workflow concurrency serializes Actions runs, not manual GitHub changes.
Network/query errors are failures, not evidence that a release is absent.

## Download and install

Download into a new directory and check both distributions before installation:

```bash
mkdir floe-v0.1.0 && cd floe-v0.1.0
gh release download v0.1.0 --repo ipdelete/floe \
  --pattern '*.whl' --pattern '*.tar.gz' --pattern SHA256SUMS
sha256sum --check SHA256SUMS
uv tool install --python 3.14 ./floe-*.whl
floe --help
```

Install the downloaded wheel, not a PyPI package named `floe` (which is a different
project). Checksums detect corruption; trust still depends on the repository and
release account. Python/systemd prerequisites still apply, and upgrading Floe
does not reinstall existing workflow units or restart a running dashboard.
Follow [Deployment](deployment.md) when unit paths/settings need updating.

## Maintenance and references

Keep pinned action revisions and the `uv` version current through reviewed PRs.
CI uses GitHub's Ubuntu 26.04 hosted image for systemd tooling and Python 3.14
environments managed by uv; generated services retain their installing environment.

The workflows use the standard patterns in
[Black's Python CLI CI](https://github.com/psf/black/blob/main/.github/workflows/test.yml),
[softprops' tag/asset release examples](https://github.com/softprops/action-gh-release#usage),
and [GitHub's artifact actions](https://github.com/actions/upload-artifact).
They do not copy Black's delete-and-recreate releases or release auto-commits.
