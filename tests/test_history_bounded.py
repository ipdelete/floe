"""Bounded history reads preserve honest partial results and default behavior."""

from unittest.mock import create_autospec

import pytest
from journal_fakes import FakeJournal, Run

import floe


@pytest.fixture
def sd():
    sd = create_autospec(floe.Systemd, instance=True)
    sd.show.return_value = {"ActiveState": "inactive", "InvocationID": ""}
    return sd


def test_log_limit_is_applied_to_the_producer_not_after_reading(sd):
    r = Run("a" * 32).succeeded_run()
    journal = FakeJournal(r.records)
    logs = floe.run_logs("mydag", r.id, journal, sd, max_records=3)
    assert journal.read == 4 and journal.limits == [4]
    assert len(logs.lines) == 3
    assert logs.run.truncated and logs.run.status == "succeeded"
    assert logs.run.started is None and logs.run.duration is None
    assert logs.run.to_dict()["truncated"] is True
    assert [line.message for line in logs.lines] == [record["MESSAGE"] for record in r.records[-3:]]
    assert logs.run.tasks[0].started is None
    assert logs.run.tasks[0].duration is None


def test_exact_limit_is_not_truncated_and_unbounded_default_is_unchanged(sd):
    r = Run("a" * 32).succeeded_run()
    journal = FakeJournal(r.records)
    bounded = floe.run_logs("mydag", r.id, journal, sd, max_records=len(r.records))
    unbounded = floe.run_logs("mydag", r.id, journal, sd)
    assert bounded == unbounded
    assert bounded.run.truncated is False and bounded.run.duration is not None
    assert journal.limits == [len(r.records) + 1, None]


def test_bounded_run_selection_does_not_fetch_every_candidate(sd):
    runs = [Run(str(index) * 32, start_us=1_800_000_000_000_000 + index * 1_000_000).succeeded_run()
            for index in range(1, 5)]
    journal = FakeJournal([record for run in runs for record in run.records])
    found = floe.list_runs("mydag", journal, sd, since="7 days ago", limit=2, max_records=3)
    assert [run.run_id for run in found] == ["4" * 32, "3" * 32]
    assert all(run.truncated and run.started is None for run in found)
    assert journal.calls == [
        ("mydag.service", "7 days ago", True, None),
        ("mydag.service", None, True, ("4" * 32,)),
        ("mydag.service", None, True, ("3" * 32,)),
    ]
    assert journal.limits == [None, 4, 4]


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_invalid_record_limits_are_rejected_before_io(sd, value):
    journal = FakeJournal([])
    with pytest.raises(floe.ValidationError, match="max_records"):
        floe.run_logs("mydag", "a" * 32, journal, sd, max_records=value)
    with pytest.raises(floe.ValidationError, match="max_records"):
        floe.list_runs("mydag", journal, sd, max_records=value)
    assert not journal.calls and not sd.mock_calls
