"""The journalctl wrapper, run against a fake journalctl executable on PATH."""

import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from floe.journal import FIELDS, Journal
from floe.model import SystemdError

SCRIPT = """#!/bin/sh
printf '%s\\n' "$@" > "{log}"
[ -n "$FAIL" ] && {{ echo "Failed to parse timestamp: $FAIL" >&2; exit 1; }}
i=0
while [ $i -lt 3 ]; do
  echo '{{"MESSAGE": "line '$i'", "__REALTIME_TIMESTAMP": "1"}}'
  # Keep the same PID and stdout open, without leaving a child to clean up.
  [ -n "$BLOCK_AFTER_FIRST" ] && exec sleep 30
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
    for name in ("FAIL", "BLOCK_AFTER_FIRST"):
        monkeypatch.delenv(name, raising=False)
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


def test_stopping_early_terminates_and_reaps_the_producer(fake, monkeypatch):
    monkeypatch.setenv("BLOCK_AFTER_FIRST", "1")
    processes = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr("floe.journal.subprocess.Popen", start)

    def consume_and_close():
        records = Journal().records("mydag.service")
        try:
            assert next(records)["MESSAGE"] == "line 0"
            [proc] = processes
            assert proc.poll() is None, "records were not yielded while the producer was running"
        finally:
            records.close()
        assert proc.returncode is not None, "close() did not reap the producer"

    # Bound both the first yield and close(): eager reads or a missing kill
    # must fail promptly rather than hang the test suite. Always clean up.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume_and_close)
        try:
            future.result(timeout=5)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)


def test_failure_raises_with_stderr(fake, monkeypatch):
    monkeypatch.setenv("FAIL", "garbage")
    with pytest.raises(SystemdError, match="Failed to parse timestamp: garbage") as caught:
        list(Journal().records("mydag.service", since="garbage"))
    assert caught.value.returncode == 1
