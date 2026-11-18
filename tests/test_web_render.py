"""Presentation contracts: local time, readable logs, disclosures, and escaping."""

import json
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest

import floe
from floe_web import render


@pytest.fixture(autouse=True)
def timezone(monkeypatch):
    try:
        with monkeypatch.context() as environment:
            def set_timezone(name):
                environment.setenv("TZ", name)
                time.tzset()

            set_timezone("UTC")
            yield set_timezone
    finally:
        time.tzset()


class Content(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.parts = []
        self.elements = []
        self.feed(source)

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    @property
    def text(self):
        return " ".join(self.parts)


def test_local_timestamps_and_explicit_offsets_follow_dst(timezone):
    timezone("America/New_York")
    winter = datetime(2026, 1, 15, 23, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 22, tzinfo=UTC)
    for value, expected, offset in ((winter, "2026-01-15 18:00:00 EST", "UTC-05:00"),
                                    (summer, "2026-07-15 18:00:00 EDT", "UTC-04:00")):
        content = Content(render.timestamp(value))
        assert expected in content.text
        [(_, attributes)] = content.elements
        assert attributes["datetime"] == value.isoformat()
        assert offset in attributes["title"]


def test_relative_dates_use_server_local_day_not_utc_day(timezone):
    timezone("America/New_York")
    now = datetime(2026, 11, 18, 0, tzinfo=UTC)  # Still Nov 17 locally.
    today = Content(render.timestamp(now + timedelta(minutes=30), brief=True, now=now))
    tomorrow = Content(render.timestamp(now + timedelta(days=1), brief=True, now=now))
    older = Content(render.timestamp(now - timedelta(days=2), brief=True, now=now))
    assert today.text == "Today 19:30"
    assert tomorrow.text == "Tomorrow 19:00"
    assert older.text == "2026-11-15 19:00"
    assert render.timestamp(None) == "—"


def test_inventory_exposes_latest_start_duration_and_mobile_labels(timezone):
    timezone("America/New_York")
    start = datetime(2020, 1, 15, 23, tzinfo=UTC)
    run = floe.Run("feedwatch", "a" * 32, "succeeded", start, start + timedelta(seconds=9.8))
    summary = floe.WorkflowSummary(floe.InstalledWorkflow("feedwatch", None, None, None),
                                   schedule=("*-*-* 08,13,18:00:00",),
                                   next_run=start + timedelta(days=1), last_run=run)
    source = render.inventory([summary])
    content = Content(source)
    assert "Succeeded" in content.text and "2020-01-15 18:00" in content.text and "9.8s" in content.text
    assert "2020-01-16 18:00" in content.text
    assert "Schedules use local time unless they specify a timezone" not in content.text
    assert "*-*-* 08,13,18:00:00" in content.text
    cells = [attrs for tag, attrs in content.elements if tag == "td"]
    assert [attrs["data-label"] for attrs in cells] == ["Workflow", "Latest run", "Schedule", "Next run"]
    assert ("table", {"class": "inventory", "role": "table"}) in content.elements
    assert any(attrs.get("href") == "/workflows/feedwatch/runs/" + "a" * 32
               for tag, attrs in content.elements if tag == "a")


@pytest.mark.parametrize("value,expected", [
    (None, "—"), (timedelta(0), "0.0s"), (timedelta(microseconds=1), "<0.1s"),
    (timedelta(milliseconds=99), "<0.1s"), (timedelta(milliseconds=100), "0.1s"),
    (timedelta(seconds=1.26), "1.3s"),
])
def test_short_durations_are_not_rounded_to_zero(value, expected):
    assert render.duration(value) == expected


def event_line(kind, when, **fields):
    event = floe.Event(kind, "mydag", "a" * 32, when.isoformat(), **fields)
    return floe.LogLine(when, "event", json.dumps(event.to_dict()), task=event.task, event=event)


def test_readable_events_are_separate_from_collapsed_escaped_raw_events():
    when = datetime(2026, 11, 17, 20, 9, 3, tzinfo=UTC)
    run = floe.Run("mydag", "a" * 32, "failed", when)
    lines = (
        event_line("run_started", when),
        event_line("task_started", when, task="<fetch>"),
        floe.LogLine(when, "output", "first line\n<script>bad()</script>", task="<fetch>"),
        event_line("task_completed", when, task="<fetch>", status="failed", error="<bad input>"),
        event_line("run_completed", when, status="failed", error="<bad input>"),
        floe.LogLine(when, "systemd", "Finished mydag.service"),
    )
    source = render.log_view(floe.RunLogs(run, lines))
    readable, raw = source.split('<details class="raw-events">')
    content = Content(readable)
    assert "Run started" in content.text and "Started" in content.text
    assert "Failed: <bad input>" in content.text and "Run failed: <bad input>" in content.text
    assert "first line\n<script>bad()</script>" in content.text
    assert "schema" not in content.text and "run_id" not in content.text
    assert "20:09:03" in content.text and "2026-11-17 UTC (UTC+00:00)" in content.text
    assert not any(tag == "script" for tag, _ in content.elements)
    assert "&lt;script&gt;" in readable and "&lt;bad input&gt;" in readable
    assert "Raw lifecycle events" in raw and "&quot;schema&quot;" in raw
    assert "&lt;fetch&gt;" in raw and "&lt;bad input&gt;" in raw
    assert "first line" not in raw and "Finished mydag.service" not in raw
    assert 'class="log-line systemd"' in readable
    assert not any("open" in attrs for tag, attrs in Content(source).elements if tag == "details")


def test_malformed_event_shaped_output_remains_output():
    when = datetime(2026, 11, 17, tzinfo=UTC)
    message = '{"event":"task_completed","schema":"invalid","status":"succeeded"}'
    logs = floe.RunLogs(floe.Run("mydag", "a" * 32, "incomplete", when),
                        (floe.LogLine(when, "output", message),))
    content = Content(render.log_view(logs))
    assert message in content.text
    assert "Raw lifecycle events" not in content.text


def test_log_day_headers_disambiguate_midnight_and_dst_fold(timezone):
    timezone("America/New_York")
    times = (datetime(2026, 10, 31, 23, tzinfo=UTC),
             datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
             datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
    logs = floe.RunLogs(floe.Run("mydag", "a" * 32, "succeeded", times[0]),
                        tuple(floe.LogLine(when, "output", "hello") for when in times))
    content = Content(render.log_view(logs))
    assert "2026-10-31 EDT (UTC-04:00)" in content.text
    assert "2026-11-01 EDT (UTC-04:00)" in content.text
    assert "2026-11-01 EST (UTC-05:00)" in content.text
    assert content.text.count("01:30:00") == 2


def test_document_keeps_updated_time_and_quiet_footer_without_repeated_limits():
    source = render.document("Workflows", "<h1>Workflows</h1>", "/").decode()
    content = Content(source)
    assert "Updated" in content.text
    footer = Content(source.split("<footer>", 1)[1].split("</footer>", 1)[0])
    assert footer.text == "Read-only · Times: UTC (UTC+00:00) (server local)."
    assert not footer.elements
    assert "History and display limits" not in content.text
    assert any(tag == "a" and attrs.get("href") == "#content" for tag, attrs in content.elements)
    assert ("main", {"id": "content", "tabindex": "-1"}) in content.elements
    assert any(tag == "time" and "datetime" in attrs and "title" in attrs
               for tag, attrs in content.elements)
    assert not any(tag in ("details", "script") for tag, _ in content.elements)


@pytest.mark.parametrize("properties,expected", [
    (None, "Timer state unavailable"),
    ({}, "Timer: activity unavailable · enablement unavailable"),
    ({"ActiveState": "active"}, "Timer: active · enablement unavailable"),
    ({"ActiveState": "active", "UnitFileState": "enabled"}, "Timer: active · enabled"),
    ({"ActiveState": "active", "UnitFileState": "disabled"}, "Timer: active · disabled"),
    ({"ActiveState": "inactive", "UnitFileState": "enabled"}, "Timer: inactive · enabled"),
    ({"ActiveState": "failed", "UnitFileState": "enabled-runtime"}, "Timer: failed · enabled-runtime"),
])
def test_timer_state_is_explicit_and_never_guessed_from_default_booleans(properties, expected):
    summary = floe.WorkflowSummary(floe.InstalledWorkflow("mydag", None, None, None),
                                   enabled=False, active=False,
                                   timer_state=None if properties is None else floe.UnitState("mydag.timer", properties))
    assert Content(render.timer_status(summary)).text == expected


def test_timer_state_output_is_escaped():
    summary = floe.WorkflowSummary(floe.InstalledWorkflow("mydag", None, None, None),
                                   timer_state=floe.UnitState("mydag.timer", {
                                       "ActiveState": "<img src=x>", "UnitFileState": "<script>"}))
    content = Content(render.timer_status(summary))
    assert "<img src=x>" in content.text and "<script>" in content.text
    assert not any(tag in ("script", "img") for tag, _ in content.elements)


def test_task_links_target_first_retained_records_without_changing_log_order():
    when = datetime(2026, 11, 17, tzinfo=UTC)
    first, second = 'fetch <"数据">', 'publish / # %'
    tasks = (floe.TaskRun(first, "succeeded"), floe.TaskRun(second, "failed"),
             floe.TaskRun("missing evidence", "incomplete"))
    run = floe.Run("mydag", "a" * 32, "incomplete", when, tasks=tasks, truncated=True)
    lines = (
        # A completion without its start is still a valid retained target.
        event_line("task_completed", when, task=first, status="succeeded"),
        floe.LogLine(when, "output", "unattributed output"),
        event_line("task_started", when, task=second),
        floe.LogLine(when, "output", "publish output", task=second),
        floe.LogLine(when, "output", "late first-task output", task=first),
    )
    source = render.run_detail(floe.RunLogs(run, lines))
    content = Content(source)
    fragment_links = [attrs["href"] for tag, attrs in content.elements
                      if tag == "a" and attrs.get("href", "").startswith("#")]
    assert "#log-0" in fragment_links and "#log-2" in fragment_links
    assert "#log-4" not in fragment_links  # Summary links to the first evidence, not the last.
    ids = [attrs["id"] for _, attrs in content.elements if "id" in attrs]
    assert len(ids) == len(set(ids))
    assert all(fragment[1:] in ids for fragment in fragment_links)
    groups = [attrs for tag, attrs in content.elements if tag == "li" and attrs.get("class") == "log-group"]
    assert [attrs["id"] for attrs in groups] == ["log-0", "log-2", "log-4"]
    assert all(attrs["tabindex"] == "-1" for attrs in groups)
    assert "Back to tasks" in content.text and "no retained log records" in content.text
    assert "Partial history" in content.text
    assert not any(tag == "数据" for tag, _ in content.elements)
    readable = source.split('<details class="raw-events">')[0]
    assert readable.index("unattributed output") < readable.index("publish output") < readable.index("late first-task output")
    assert "floe logs --name mydag " + "a" * 32 in content.text


def test_run_heading_identifies_outcome_and_keeps_workflow_in_breadcrumb():
    when = datetime(2026, 11, 17, tzinfo=UTC)
    source = render.run_detail(floe.RunLogs(floe.Run("report.py", "a" * 32, "failed", when), ()))
    heading = source.split("<h1>", 1)[1].split("</h1>", 1)[0]
    assert Content(heading).text.strip() == "Run failed" and 'class="failed"' in heading
    assert 'href="/workflows/report.py"' in source and "report.py" in Content(source).text


def test_cli_help_quotes_arguments_and_preserves_explicit_name_targeting():
    from shlex import split

    content = Content(render.inspection_help("report.py", "a" * 32))
    assert "floe status --name report.py" in content.text
    assert "floe logs --name report.py " + "a" * 32 in content.text
    # Defend copyable shell text as well as HTML, even for malformed metadata.
    command_text = Content(render.inspection_help("bad; echo injected", "a" * 32)).text
    commands = command_text.splitlines()
    assert split(commands[0]) == ["floe", "status", "--name", "bad; echo injected"]
