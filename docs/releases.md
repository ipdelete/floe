# Releases

## Download and install

Get Floe from [GitHub Releases](https://github.com/ipdelete/floe/releases), not PyPI. The PyPI package named `floe` is a different project.

Download into an empty directory, replacing `v0.1.0` with the release you want:

```bash
gh release download v0.1.0 --repo ipdelete/floe \
  --pattern '*.whl' --pattern '*.tar.gz' --pattern SHA256SUMS
sha256sum --check SHA256SUMS
```

If both files pass the check, install the wheel:

```bash
uv tool install --python 3.14 ./floe-*.whl
floe --help
```

Upgrading Floe doesn't reinstall workflow units or restart a running dashboard. See [deployment](deployment.md#upgrade-existing-installations).

## Version policy

Before 1.0, use patches for compatible fixes and minor versions for features or breaking changes. After 1.0, use [SemVer](https://semver.org/). Public contracts include the Python API, CLI arguments and exit codes, JSON and events, and workflow and deployment behavior. Explain breaking changes and how to upgrade in the release notes.

| Git tag | Package and lockfile version | Changelog heading |
| --- | --- | --- |
| `v0.2.0` | `0.2.0` | `## [0.2.0] - YYYY-MM-DD` |
| `v0.2.0-alpha.1` | `0.2.0a1` | `## [0.2.0-alpha.1] - YYYY-MM-DD` |
| `v0.2.0-beta.1` | `0.2.0b1` | `## [0.2.0-beta.1] - YYYY-MM-DD` |
| `v0.2.0-rc.1` | `0.2.0rc1` | `## [0.2.0-rc.1] - YYYY-MM-DD` |

No leading zeroes, other prerelease labels, or build metadata. Alpha, beta, and RC releases are GitHub prereleases.

## Prepare a release

1. Start a release branch from current `master`. For example:

   ```bash
   git switch master
   git pull --ff-only
   git switch -c release/0.2.0
   uv version 0.2.0 --no-sync
   uv lock --check
   uv sync --locked
   ```

2. Move the release notes from **Unreleased** into one nonempty, dated version section in `CHANGELOG.md`. Leave an **Unreleased** section for later changes.
3. Run [local checks](../AGENTS.md#testing-and-validation), open a PR, and merge it after review and CI pass.
4. Tag the exact merged commit on `master`, not the release branch:

   ```bash
   git switch master
   git pull --ff-only
   git tag -a v0.2.0 MERGED_COMMIT -m 'Floe 0.2.0'
   git push origin v0.2.0
   ```

5. In **Actions → Release**, review the commit, notes, checks, and saved artifact. Then approve the **`release`** environment deployment. The publish job uses the validated files, not a rebuild.
6. Check the published notes, wheel, source archive, `SHA256SUMS`, and prerelease flag. Never move a published tag, replace its files, or reuse its version for different files.

An ordinary merge doesn't publish a release or change any installed workflows.

## One-time repository setup

- Create a GitHub environment named `release` with at least one required reviewer. If there's only one maintainer, allow self-approval. The workflow stops if it can't verify this setup.
- Restrict the environment to the intended `v*` tags. Limit who can create release tags and prevent moving or deleting them. Protect `master` with CI and review as appropriate.
- Enable immutable releases if available. Allow the pinned `actions/*`, `astral-sh/setup-uv`, and `softprops/action-gh-release` actions.

Publishing uses the built-in `GITHUB_TOKEN`. Only the publish job has `contents: write`.

## Recover a failed release

Keep the original Actions artifact. It contains the validated files and expires after 30 days. Don't edit a draft while the job is running.

- **No release or draft:** fix the problem and choose **Re-run failed jobs**. If validation passed, keep its original artifact; don't rerun all jobs or move the tag.
- **A draft exists:** the job won't take it over. Download the original artifact with `gh run download RUN_ID --name ARTIFACT_NAME --dir recovery` and check its `SHA256SUMS`. Download and compare the draft files with the originals. Stop on any mismatch. Upload only missing files with `gh release upload TAG FILE`, never `--clobber`. Download the full set again and verify it against the original checksums. Confirm the remote tag, notes, and prerelease flag before publishing the draft.
- **Already published:** verify it. Don't replace files or delete the release. Publish a new version if a correction is needed.
- **Original artifact unavailable:** if you can't recover the validated files, use a new version. Don't rebuild under the old tag.
