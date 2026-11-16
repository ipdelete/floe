import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import call, create_autospec

import pytest

from floe.deploy import Deployment, cli, install, uninstall
from floe.store import NotOwnedError, UnitStore
from floe.systemd import Systemd, SystemdError

ROOT = Path(__file__).parent.parent
LEGACY = Path(__file__).parent / "fixtures" / "legacy"


@pytest.fixture
def dep(tmp_path):
    script = tmp_path / "my flows" / "workflow.py"
    script.parent.mkdir()
    script.touch()
    return Deployment("mydag", script, ["hourly"])


@pytest.fixture
def sd():
    return create_autospec(Systemd, instance=True)


def test_install_then_reinstall_is_idempotent(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    assert install(dep, store, sd) == ["mydag.service", "mydag.timer"]
    assert sd.mock_calls == [call.daemon_reload(), call.enable("mydag.timer", now=True)]
    mtimes = {name: (store.path / name).stat().st_mtime_ns for name in dep.render_units()}
    sd.reset_mock()
    assert install(dep, store, sd) == []
    assert sd.mock_calls == [call.enable("mydag.timer", now=True)]
    assert mtimes == {name: (store.path / name).stat().st_mtime_ns for name in dep.render_units()}


def test_install_refuses_foreign_units_without_adopt(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    store.path.mkdir()
    foreign = (LEGACY / "mydag.timer").read_text()
    (store.path / "mydag.timer").write_text(foreign)
    with pytest.raises(NotOwnedError, match="--adopt"):
        install(dep, store, sd)
    assert not (store.path / "mydag.service").exists()
    assert (store.path / "mydag.timer").read_text() == foreign
    assert sd.mock_calls == []
    assert install(dep, store, sd, adopt=True) == ["mydag.service", "mydag.timer"]
    assert store.is_owned("mydag.timer")


@pytest.mark.parametrize("adopt", [False, True])
@pytest.mark.parametrize("destination", ["mask", "dangling", "directory"])
def test_all_destinations_preflighted_before_writes(dep, tmp_path, sd, adopt, destination):
    store = UnitStore(tmp_path / "units")
    store.path.mkdir()
    timer = store.path / dep.timer_name
    if destination == "directory":
        timer.mkdir()
    else:
        timer.symlink_to("/dev/null" if destination == "mask" else store.path / "absent")
    with pytest.raises(NotOwnedError):
        install(dep, store, sd, adopt=adopt)
    assert not (store.path / dep.service_name).exists()
    assert sd.mock_calls == []


def test_render_failure_writes_nothing(dep, tmp_path, sd):
    dep = Deployment(dep.workflow, dep.script, dep.schedule, timeout="bad\nvalue")
    store = UnitStore(tmp_path / "units")
    with pytest.raises(ValueError):
        install(dep, store, sd)
    assert not store.path.exists()
    assert sd.mock_calls == []


def test_uninstall(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    assert uninstall(dep, store, sd) == ["mydag.timer", "mydag.service"]
    assert sd.mock_calls == [call.disable("mydag.timer", now=True), call.stop("mydag.service"), call.daemon_reload()]
    assert list(store.path.iterdir()) == []
    sd.reset_mock()
    assert uninstall(dep, store, sd) == []
    assert sd.mock_calls == []


def test_uninstall_leaves_foreign_files_and_systemd_alone(dep, tmp_path, sd):
    store = UnitStore(tmp_path)
    (tmp_path / dep.service_name).write_text("[Service]\nExecStart=/bin/true\n")
    (tmp_path / dep.timer_name).symlink_to("/dev/null")
    assert uninstall(dep, store, sd) == []
    assert sd.mock_calls == []
    assert (tmp_path / dep.service_name).exists()
    assert (tmp_path / dep.timer_name).is_symlink()


def test_cli_dry_run_prints_exact_files(dep, tmp_path, sd, capsys):
    store = UnitStore(tmp_path / "units")
    assert cli(dep, ["install", "--dry-run"], store=store, sd=sd) == 0
    out = capsys.readouterr().out
    for name, text in dep.render_units().items():
        assert f"# {store.path / name}\n{text}" in out
    assert not store.path.exists()
    assert sd.mock_calls == []


def test_cli_reports_ownership_error(dep, tmp_path, sd, capsys):
    store = UnitStore(tmp_path)
    (tmp_path / dep.service_name).write_text("foreign")
    assert cli(dep, ["install"], store=store, sd=sd) == 1
    assert "--adopt" in capsys.readouterr().err


@pytest.mark.parametrize("error", [OSError("disk error"), ValueError("bad value"),
                                   SystemdError(["systemctl"], 1, "no session")])
def test_cli_reports_expected_failures(dep, tmp_path, sd, capsys, error):
    sd.start.side_effect = error
    assert cli(dep, ["start"], store=UnitStore(tmp_path), sd=sd) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "Traceback" not in err


def test_cli_start_waits_for_service(dep, tmp_path, sd, capsys):
    assert cli(dep, ["start"], store=UnitStore(tmp_path), sd=sd) == 0
    assert sd.mock_calls == [call.start(dep.service_name)]
    assert "finished successfully" in capsys.readouterr().out


def test_cli_status(dep, tmp_path, sd, capsys):
    sd.show.return_value = {"ActiveState": "inactive"}
    assert cli(dep, ["status"], store=UnitStore(tmp_path), sd=sd) == 0
    assert [c.args[0] for c in sd.show.call_args_list] == [dep.service_name, dep.timer_name]
    assert "ActiveState=inactive" in capsys.readouterr().out


def test_default_unit_dir_resolved_at_call_time(dep, tmp_path, sd, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom"))
    assert cli(dep, ["install", "--dry-run"], sd=sd) == 0
    assert str(tmp_path / "custom" / "systemd" / "user" / dep.service_name) in capsys.readouterr().out
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli(dep, ["install", "--dry-run"], sd=sd) == 0
    assert str(tmp_path / "home" / ".config" / "systemd" / "user" / dep.service_name) in capsys.readouterr().out
    assert sd.mock_calls == []


def test_workflow_runs_with_system_python_without_floe():
    r = subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "workflow.py"), "run"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert '"event": "run_completed"' in r.stdout


def test_workflow_deploy_commands_explain_missing_floe():
    r = subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "workflow.py"), "status"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 1 and "uv run" in r.stderr


def test_workflow_dry_run_via_venv(tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, str(ROOT / "workflow.py"), "install", "--dry-run"],
                       capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr
    assert f"ExecStart=/usr/bin/python3 {ROOT / 'workflow.py'} run" in r.stdout
    assert not (tmp_path / "systemd").exists()


@pytest.mark.parametrize("old,new", [('WORKFLOW = "mydag"', 'WORKFLOW = "../bad"'),
                                    ('SCHEDULE = ["hourly"]', 'SCHEDULE = []')])
def test_workflow_configuration_errors_are_concise(tmp_path, old, new):
    script = tmp_path / "workflow.py"
    script.write_text((ROOT / "workflow.py").read_text().replace(old, new))
    r = subprocess.run([sys.executable, str(script), "install", "--dry-run"], capture_output=True, text=True)
    assert r.returncode == 1
    assert "error:" in r.stderr and "Traceback" not in r.stderr
