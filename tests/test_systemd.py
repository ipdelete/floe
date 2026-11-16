import subprocess
from unittest.mock import Mock

import pytest

from floe.systemd import Systemd, SystemdError


@pytest.mark.parametrize("method,args,kwargs,command", [
    ("daemon_reload", (), {}, ["daemon-reload"]),
    ("enable", ("demo.timer",), {"now": True}, ["enable", "--now", "demo.timer"]),
    ("disable", ("demo.timer",), {"now": True}, ["disable", "--now", "demo.timer"]),
    ("start", ("demo.service",), {}, ["start", "demo.service"]),
    ("start", ("demo.service",), {"wait": False}, ["start", "--no-block", "demo.service"]),
    ("stop", ("demo.service",), {}, ["stop", "demo.service"]),
])
def test_systemctl_commands(monkeypatch, method, args, kwargs, command):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("floe.systemd.subprocess.run", run)
    getattr(Systemd(), method)(*args, **kwargs)
    run.assert_called_once_with(["systemctl", "--user", *command], capture_output=True, text=True)


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
