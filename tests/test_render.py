import shutil
import subprocess
from pathlib import Path

import pytest

from floe.deploy import Deployment
from floe.render import environment, exec_line


@pytest.mark.parametrize("argv,expected", [
    (["/bin/echo", "two words"], '/bin/echo "two words"'),
    (["/bin/echo", 'say "hi"'], r'/bin/echo "say \"hi\""'),
    (["/bin/date", "+%F"], "/bin/date +%%F"),
    (["/bin/echo", "$HOME"], "/bin/echo $$HOME"),
    (["/bin/echo", ";"], r"/bin/echo \;"),
    (["/bin/echo", ""], '/bin/echo ""'),
    (["/bin/echo", "a\\b"], r"/bin/echo a\\b"),
])
def test_command_quoting(argv, expected):
    assert exec_line(argv) == expected


@pytest.mark.parametrize("argv", [[], ["/bin/echo", "a\nb"], ["/bin/echo", "nul\x00"]])
def test_invalid_commands_rejected(argv):
    with pytest.raises(ValueError):
        exec_line(argv)


def test_nonpersistent_timer(tmp_path):
    text = Deployment("demo", tmp_path / "workflow.py", ["daily"], persistent=False).render_units()["demo.timer"]
    assert "Persistent=no\n" in text


def test_render_units_is_deterministic_and_preserves_service_settings(tmp_path):
    dep = Deployment("demo", tmp_path / "my flows" / "workflow.py", ["hourly", "daily"])
    rendered = dep.render_units()
    assert rendered == dep.render_units()
    assert set(rendered) == {"demo.service", "demo.timer"}
    service = rendered["demo.service"]
    assert f'ExecStart=/usr/bin/python3 -m floe_cli run "{dep.script.resolve()}"\n' in service
    assert f'Environment="PYTHONPATH={Path(__file__).parents[1] / "src"}"\n' in service
    for setting in ("Type=oneshot", "TimeoutStartSec=30min", "KillMode=control-group",
                    "StandardOutput=journal", "StandardError=journal", "SyslogIdentifier=demo"):
        assert f"{setting}\n" in service
    timer = rendered["demo.timer"]
    assert "OnCalendar=hourly\nOnCalendar=daily\n" in timer
    assert "Persistent=yes\n" in timer
    assert "Unit=demo.service\n" in timer
    assert "WantedBy=timers.target\n" in timer
    for text in rendered.values():
        assert "[X-Floe]\nOwner=floe\nWorkflow=demo\n" in text


def test_literal_percent_paths_are_escaped_in_both_directives(tmp_path):
    script = tmp_path / "percent%h and $HOME" / "workflow.py"
    service = Deployment("demo", script, ["hourly"]).render_units()["demo.service"]
    assert f'ExecStart=/usr/bin/python3 -m floe_cli run "{str(script).replace("%", "%%").replace("$", "$$")}"\n' in service
    assert f'WorkingDirectory={str(script.parent).replace("%", "%%")}\n' in service


@pytest.mark.parametrize("name", ["", "../escape", "bad/name", "-option", "space name", "x\nX=y", "x@", "x" * 248])
def test_invalid_workflow_names_fail_at_construction(tmp_path, name):
    with pytest.raises(ValueError, match="workflow"):
        Deployment(name, tmp_path / "workflow.py", ["hourly"])


@pytest.mark.parametrize("schedule", [[], [""], ["hourly\nUnit=other.service"], [" hourly"], ["daily\\"], "hourly"])
def test_invalid_schedule_fails_at_construction(tmp_path, schedule):
    with pytest.raises(ValueError, match="schedule"):
        Deployment("demo", tmp_path / "workflow.py", schedule)


def test_schedule_is_an_immutable_snapshot(tmp_path):
    schedule = ["hourly"]
    dep = Deployment("demo", tmp_path / "workflow.py", schedule)
    schedule.append("daily")
    assert dep.schedule == ("hourly",)


def test_environment_quoting():
    assert environment("PYTHONPATH", "/a b/$HOME/100%") == 'Environment="PYTHONPATH=/a b/$HOME/100%%"'
    for value in ['/a"b', "/a\\b", "/a\nb", ""]:
        with pytest.raises(ValueError):
            environment("PYTHONPATH", value)


@pytest.mark.parametrize("field,value", [
    ("timeout", "30min\nExecStart=/bad"),
    ("pythonpath", Path("relative/src")),
    ("pythonpath", Path('/with "quote"')),
    ("python", "relative-python"),
    ("script", Path("/tmp/new\nline/workflow.py")),
])
def test_invalid_render_values_are_rejected(tmp_path, field, value):
    kwargs = {"workflow": "demo", "script": tmp_path / "workflow.py", "schedule": ["hourly"], field: value}
    with pytest.raises(ValueError):
        Deployment(**kwargs).render_units()


def test_rendered_units_pass_systemd_verification(tmp_path):
    analyze = shutil.which("systemd-analyze")
    if not analyze:
        pytest.skip("systemd-analyze not available")
    script = tmp_path / "percent%h and spaces" / "workflow.py"
    script.parent.mkdir()
    script.touch()
    dep = Deployment("demo", script, ["*-*-* 08:00:00"], pythonpath=tmp_path / "src $HOME 100%")
    files = []
    for name, text in dep.render_units().items():
        path = tmp_path / name
        path.write_text(text)
        files.append(str(path))
    result = subprocess.run([analyze, "--user", "verify", *files], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
