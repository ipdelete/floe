"""Check the small tag/version/notes gate embedded in the Actions workflow."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def release_check(tmp_path):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    block = workflow.split("# BEGIN release-version-check\n", 1)[1].split("# END release-version-check", 1)[0]
    script = textwrap.dedent(block).strip() + "\n"
    runner = tmp_path / "runner"
    runner.mkdir()

    def check(tag="v0.2.0", version="0.2.0", *, locked=None, changelog=None):
        (tmp_path / "pyproject.toml").write_text(f'[project]\nname="floe"\nversion="{version}"\n')
        (tmp_path / "uv.lock").write_text(
            f'[[package]]\nname="floe"\nversion="{version if locked is None else locked}"\nsource={{editable="."}}\n')
        (tmp_path / "CHANGELOG.md").write_text(
            f'## [{tag[1:]}] - 2026-11-18\n\nRelease notes.\n' if changelog is None else changelog)
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script], cwd=tmp_path,
            env={**os.environ, "RELEASE_TAG": tag, "RUNNER_TEMP": str(runner),
                 "GITHUB_OUTPUT": str(runner / "outputs")},
            capture_output=True, text=True, timeout=10,
        )

    return check, runner


@pytest.mark.parametrize("tag,version,prerelease", [
    ("v0.2.0", "0.2.0", False), ("v1.0.0", "1.0.0", False),
    ("v1.2.3-alpha.0", "1.2.3a0", True), ("v1.2.3-beta.2", "1.2.3b2", True),
    ("v1.2.3-rc.1", "1.2.3rc1", True),
])
def test_release_tag_version_mapping_and_exact_notes(release_check, tag, version, prerelease):
    check, runner = release_check
    changelog = f"## [Unreleased]\nNot yet.\n\n## [{tag[1:]}]\n\nChosen notes.\n\n## [0.0.1]\nOld.\n"
    result = check(tag, version, changelog=changelog)
    assert result.returncode == 0, result.stderr
    assert (runner / "release-notes.md").read_text() == "Chosen notes.\n"
    assert (runner / "outputs").read_text() == f"prerelease={str(prerelease).lower()}\n"


@pytest.mark.parametrize("tag", ["0.2.0", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3-rc.01",
                                  "v1.2.3-preview.1", "v1.2.3+build.1", "v1.2.3rc1", "v1.2", "v1.2.3\nevil"])
def test_noncanonical_or_unsupported_release_tag_fails(release_check, tag):
    check, runner = release_check
    assert check(tag).returncode != 0
    assert not (runner / "release-notes.md").exists()


@pytest.mark.parametrize("kwargs", [
    {"version": "0.3.0"}, {"locked": "0.1.0"}, {"changelog": "## [Unreleased]\nNotes.\n"},
    {"changelog": "## [0.2.0]\n\n"}, {"changelog": "## [0.2.0]\nOne.\n## [0.2.0]\nTwo.\n"},
])
def test_metadata_drift_or_missing_notes_blocks_release(release_check, kwargs):
    check, runner = release_check
    assert check(**kwargs).returncode != 0
    assert not (runner / "outputs").exists()
