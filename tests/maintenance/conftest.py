"""Real local Git remotes; stand-ins only for dependency/model services."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def checkout(tmp_path):
    def git(repo, *args):
        return subprocess.run(["/usr/bin/git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()

    upstream = tmp_path / "upstream.git"
    origin = tmp_path / "origin.git"
    for repo in (upstream, origin):
        repo.mkdir()
        git(repo, "init", "--bare", "--initial-branch=main")
    author = tmp_path / "author"
    author.mkdir()
    git(author, "init", "--initial-branch=main")
    git(author, "config", "user.name", "Floe test")
    git(author, "config", "user.email", "floe@example.invalid")
    (author / "version.txt").write_text("old\n")
    git(author, "add", ".")
    git(author, "commit", "-m", "initial")
    for repo in (upstream, origin):
        git(author, "push", str(repo), "main")
    old = git(author, "rev-parse", "HEAD")
    local = tmp_path / "checkout"
    git(tmp_path, "clone", str(origin), str(local))
    git(local, "remote", "add", "upstream", str(upstream))
    (author / "version.txt").write_text("new\n")
    git(author, "commit", "-am", "upstream update")
    git(author, "push", str(upstream), "main")
    target = git(author, "rev-parse", "HEAD")

    failure = tmp_path / "doctor-fails"
    calls = tmp_path / "calls"
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/sh\necho sync >> '{calls}'\n")
    uv.chmod(0o755)
    doctor = tmp_path / "pr-agent"
    doctor.write_text(f"#!/bin/sh\necho doctor >> '{calls}'\n"
                      f"if test -f '{failure}'; then echo 'model unavailable' >&2; exit 1; fi\n"
                      "echo '3 of 3 checks passed'\n")
    doctor.chmod(0o755)
    workflow = tmp_path / "maintenance.py"
    source = Path("examples/pr_agent/maintenance.py").read_text()
    workflow.write_text(source + f"\nREPO = Path({str(local)!r})\nUV = Path({str(uv)!r})\n"
                        f"DOCTOR = Path({str(doctor)!r})\nGH = GIT\n"
                        "retries = {name: RetryPolicy(max_attempts=2, delay_seconds=0,\n"
                        "    retry_on=(TransientFailure,)) for name in retries}\n")
    return SimpleNamespace(git=git, local=local, origin=origin, upstream=upstream,
                           author=author, old=old, target=target, workflow=workflow,
                           failure=failure, calls=calls)
