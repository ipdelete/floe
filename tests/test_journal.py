"""The journalctl wrapper, run against a fake journalctl executable on PATH."""

import json
import os
import stat

import pytest

from floe.journal import FIELDS, Journal
from floe.model import SystemdError

SCRIPT = """#!/bin/sh
printf '%s\\n' "$@" > "{log}"
[ -n "$FAIL" ] && {{ echo "Failed to parse timestamp: $FAIL" >&2; exit 1; }}
i=0
while [ $i -lt "${{COUNT:-3}}" ]; do
  echo '{{"MESSAGE": "line '$i'", "__REALTIME_TIMESTAMP": "1"}}'
  i=$((i+1))
done
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    log = tmp_path / "args"
    script = tmp_path / "journalctl"
    script.write_text(SCRIPT.format(log=log))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return log


def test_records_command_and_parsing(fake):
    records = list(Journal().records("mydag.service", since="24 hours ago", reverse=True))
    assert [r["MESSAGE"] for r in records] == ["line 0", "line 1", "line 2"]
    assert fake.read_text().splitlines() == [
        "--user", "--unit=mydag.service", "--output=json", "--no-pager",
        f"--output-fields={','.join(FIELDS)}", "--since=24 hours ago", "--reverse"]


def test_default_is_oldest_first_without_since(fake):
    list(Journal().records("mydag.service"))
    args = fake.read_text().splitlines()
    assert "--reverse" not in args and not any(a.startswith("--since") for a in args)


def test_stopping_early_does_not_raise_or_hang(fake, monkeypatch):
    monkeypatch.setenv("COUNT", "100000")
    records = Journal().records("mydag.service")
    assert next(records)["MESSAGE"] == "line 0"
    records.close()


def test_failure_raises_with_stderr(fake, monkeypatch):
    monkeypatch.setenv("FAIL", "garbage")
    with pytest.raises(SystemdError, match="Failed to parse timestamp: garbage") as caught:
        list(Journal().records("mydag.service", since="garbage"))
    assert caught.value.returncode == 1


def test_records_are_json(fake):
    assert all(isinstance(r, dict) for r in Journal().records("x.service"))
    json.dumps(list(Journal().records("x.service")))
