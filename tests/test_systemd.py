import subprocess
from unittest.mock import Mock, call

import pytest

from floe.model import ValidationError
from floe.systemd import Systemd, SystemdError, validate_calendars, validate_timeout


def test_calendars_use_systemd_parser_without_a_service_manager(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("floe.systemd.subprocess.run", run)
    validate_calendars(("hourly", "2000-01-01"))
    assert run.call_args_list == [
        call(["systemd-analyze", "calendar", "--iterations=0", "--", expression],
             capture_output=True, text=True, timeout=10)
        for expression in ("hourly", "2000-01-01")
    ]


@pytest.mark.parametrize("error", [FileNotFoundError("missing analyzer"),
                                   subprocess.TimeoutExpired("systemd-analyze", 10)])
def test_calendar_validation_tool_failure_is_actionable(monkeypatch, error):
    monkeypatch.setattr("floe.systemd.subprocess.run", Mock(side_effect=error))
    with pytest.raises(ValidationError, match="cannot validate schedule with systemd-analyze"):
        validate_calendars(("hourly",))


@pytest.mark.parametrize("timeout", ["30min", "1h 30min", "0", "infinity"])
def test_timeout_uses_systemd_parser_without_a_service_manager(monkeypatch, timeout):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("floe.systemd.subprocess.run", run)
    validate_timeout(timeout)
    run.assert_called_once_with(["systemd-analyze", "timespan", "--", timeout],
                                capture_output=True, text=True, timeout=10)


def test_invalid_timeout_preserves_parser_diagnostics(monkeypatch):
    monkeypatch.setattr("floe.systemd.subprocess.run", Mock(
        return_value=subprocess.CompletedProcess([], 1, "", "Failed to parse time span")))
    with pytest.raises(ValidationError, match="invalid timeout 'bad': Failed to parse time span"):
        validate_timeout("bad")


@pytest.mark.parametrize("error", [FileNotFoundError("missing analyzer"),
                                   subprocess.TimeoutExpired("systemd-analyze", 10)])
def test_timeout_validation_tool_failure_is_actionable(monkeypatch, error):
    monkeypatch.setattr("floe.systemd.subprocess.run", Mock(side_effect=error))
    with pytest.raises(ValidationError, match="cannot validate timeout with systemd-analyze"):
        validate_timeout("30min")


@pytest.mark.parametrize("method,args,kwargs,command", [
    ("daemon_reload", (), {}, ["daemon-reload"]),
    ("enable", ("demo.timer",), {"now": True}, ["enable", "--now", "demo.timer"]),
    ("disable", ("demo.timer",), {"now": True}, ["disable", "--now", "demo.timer"]),
    ("start", ("demo.service",), {}, ["start", "demo.service"]),
    ("start", ("demo.service",), {"wait": False}, ["start", "--no-block", "demo.service"]),
    ("stop", ("demo.service",), {}, ["stop", "demo.service"]),
    ("reset_failed", ("demo.service",), {}, ["reset-failed", "demo.service"]),
    ("clean_state", ("demo.timer",), {}, ["clean", "--what=state", "demo.timer"]),
])
def test_systemctl_commands(monkeypatch, method, args, kwargs, command):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("floe.systemd.subprocess.run", run)
    getattr(Systemd(), method)(*args, **kwargs)
    run.assert_called_once_with(["systemctl", "--user", *command], capture_output=True, text=True)


def test_show_all_keeps_repeated_properties_and_unix_timestamps(monkeypatch):
    out = "TimersCalendar={ OnCalendar=a ; next_elapse=@1 }\nTimersCalendar={ OnCalendar=b ; next_elapse=@2 }\n"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, out, ""))
    monkeypatch.setattr("floe.systemd.subprocess.run", run)
    assert [v for _, v in Systemd().show_all("demo.timer", "TimersCalendar", unix_timestamps=True)] == [
        "{ OnCalendar=a ; next_elapse=@1 }", "{ OnCalendar=b ; next_elapse=@2 }"]
    run.assert_called_once_with(
        ["systemctl", "--user", "show", "demo.timer", "--property=TimersCalendar", "--timestamp=unix"],
        capture_output=True, text=True,
    )


def test_show_parses_values_containing_equals(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "ActiveState=inactive\nExtra=a=b\n", ""))
    monkeypatch.setattr("floe.systemd.subprocess.run", run)
    assert Systemd().show("demo.service", "ActiveState", "Extra") == {"ActiveState": "inactive", "Extra": "a=b"}
    run.assert_called_once_with(
        ["systemctl", "--user", "show", "demo.service", "--property=ActiveState,Extra"],
        capture_output=True, text=True,
    )


def test_systemctl_failure_preserves_diagnostics(monkeypatch):
    monkeypatch.setattr("floe.systemd.subprocess.run", Mock(
        return_value=subprocess.CompletedProcess([], 1, "", "no user session\n"),
    ))
    with pytest.raises(SystemdError, match="no user session") as caught:
        Systemd().start("demo.service")
    assert caught.value.returncode == 1
    assert caught.value.stderr == "no user session\n"


def test_stop_missing_ok_accepts_unloaded_unit(monkeypatch):
    monkeypatch.setattr("floe.systemd.subprocess.run", Mock(
        return_value=subprocess.CompletedProcess([], 5, "", "Unit demo.service not loaded.\n"),
    ))
    Systemd().stop("demo.service", missing_ok=True)
    with pytest.raises(SystemdError, match="not loaded"):
        Systemd().stop("demo.service")
