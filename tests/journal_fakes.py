"""Shared test helpers: a fake Journal and a builder for one invocation's records.
systemd message shapes match the real captured records in tests/fixtures."""

import json
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MANAGER = {"_TRANSPORT": "journal", "SYSLOG_IDENTIFIER": "systemd", "PRIORITY": "6"}


def fixture(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


class FakeJournal:
    """Records oldest first, like the journal; counts how many were read."""

    def __init__(self, records):
        self.records_list = records
        self.calls = []
        self.read = 0

    def records(self, unit, *, since=None, reverse=False, invocations=None):
        self.calls.append((unit, since, reverse, invocations))
        for record in (self.records_list[::-1] if reverse else self.records_list):
            invocation = record.get("_SYSTEMD_INVOCATION_ID") or record.get("USER_INVOCATION_ID")
            if invocations is None or invocation in invocations:
                self.read += 1
                yield record


class Run:
    """Builds the journal records of one invocation."""

    def __init__(self, invocation, start_us=1_800_000_000_000_000):
        self.id = invocation
        self.us = start_us
        self.records = []

    def _add(self, record):
        self.us += 1000
        self.records.append({"__REALTIME_TIMESTAMP": str(self.us), **record})
        return self

    def systemd(self, message, **fields):
        return self._add({**MANAGER, "USER_INVOCATION_ID": self.id, "MESSAGE": message, **fields})

    def output(self, message):
        return self._add({"_TRANSPORT": "stdout", "SYSLOG_IDENTIFIER": "mydag", "PRIORITY": "6",
                          "_SYSTEMD_INVOCATION_ID": self.id, "MESSAGE": message})

    def event(self, event, run_id=None, **fields):
        seconds = self.us / 1e6 + 0.0005
        timestamp = datetime.fromtimestamp(seconds, UTC).isoformat()
        return self.output(json.dumps({"schema": 1, "event": event, "workflow": "mydag",
                                       "run_id": run_id or self.id, "timestamp": timestamp, **fields}))

    def starting(self):
        return self.systemd("Starting mydag.service - Floe workflow mydag...",
                            MESSAGE_ID="7d4958e842da4a758f6c1cdc7b36dcc5", JOB_TYPE="start")

    def finished(self):
        return self.systemd("Finished mydag.service - Floe workflow mydag.",
                            MESSAGE_ID="39f53479d3a045ac8e11786248231fbf", JOB_TYPE="start", JOB_RESULT="done")

    def failed(self, result="exit-code"):
        self.systemd("mydag.service: Main process exited, code=exited, status=1/FAILURE",
                     MESSAGE_ID="98e322203f7a4ed290d09fe03c09fe15", EXIT_CODE="exited", EXIT_STATUS="1")
        return self.systemd(f"mydag.service: Failed with result '{result}'.",
                            MESSAGE_ID="d9b373ed55a64feb8242e02dbe79a49c", UNIT_RESULT=result)

    def succeeded_run(self, tasks=("fetch", "transform")):
        self.starting().event("run_started")
        for task in tasks:
            self.event("task_started", task=task).output(f"doing {task}")
            self.event("task_completed", task=task, status="succeeded")
        return self.event("run_completed", status="succeeded").finished()
