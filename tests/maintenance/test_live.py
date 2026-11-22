"""Scheduled-service path: Git update outcome is visible in journal-backed history."""

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.live
FLOE = str(Path(sys.executable).parent / "floe")


def test_failed_doctor_is_visible_and_next_run_publishes(checkout):
    c = checkout
    name = f"pr-agent-test-{uuid.uuid4().hex[:12]}"
    with c.workflow.open("a") as file:
        file.write(f"\nWORKFLOW = {name!r}\nSCHEDULE = ['2099-01-01']\n")

    def cli(*args, check=True):
        return subprocess.run([FLOE, *args], check=check, capture_output=True,
                              text=True, timeout=120)

    try:
        cli("install", str(c.workflow))
        c.failure.touch()
        failed = cli("start", name, check=False)
        assert failed.returncode != 0
        runs = json.loads(cli("runs", name, "--json", "-n", "1").stdout)
        assert runs[0]["status"] == "failed"
        assert c.git(c.origin, "rev-parse", "main") == c.old
        assert c.git(c.local, "rev-parse", "HEAD") == c.target
        assert "model unavailable" in cli("logs", name).stdout

        c.failure.unlink()
        cli("start", name)
        runs = json.loads(cli("runs", name, "--json", "-n", "1").stdout)
        assert runs[0]["status"] == "succeeded"
        assert c.git(c.origin, "rev-parse", "main") == c.target
    finally:
        cli("uninstall", name, check=False)
