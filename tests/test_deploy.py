import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import call, create_autospec

import pytest

from floe.deploy import Deployment, UninstallError, cli, install, uninstall
from floe.store import NotOwnedError, UnitStore
from floe.systemd import Systemd, SystemdError

ROOT = Path(__file__).parent.parent


@pytest.fixture
def dep(tmp_path):
    script = tmp_path / "my flows" / "workflow.py"
    script.parent.mkdir()
    script.touch()
    return Deployment("mydag", script, ["hourly"])


@pytest.fixture
def sd():
    sd = create_autospec(Systemd, instance=True)
    sd.show.return_value = {"LoadState": "not-found", "ActiveState": "inactive", "FragmentPath": ""}
    return sd


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
    foreign = "[Timer]\nOnCalendar=daily\n"
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
    assert [c for c in sd.mock_calls if c[0] != "show"] == [
        call.disable("mydag.timer", now=True),
        call.stop("mydag.service", missing_ok=True),
        call.daemon_reload(),
    ]
    assert list(store.path.iterdir()) == []
    sd.reset_mock()
    assert uninstall(dep, store, sd) == []
    assert {c[0] for c in sd.mock_calls} == {"show"}  # only checks for stale units


def _install_enabled(dep, store, sd):
    install(dep, store, sd)
    wants = store.path / "timers.target.wants"
    wants.mkdir()
    (wants / dep.timer_name).symlink_to(store.path / dep.timer_name)
    sd.reset_mock()
    return wants / dep.timer_name


def test_uninstall_continues_after_stop_failure_and_reports(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    link = _install_enabled(dep, store, sd)
    sd.stop.side_effect = SystemdError(["systemctl", "stop"], 1, "stop timed out")
    with pytest.raises(UninstallError, match="stop timed out") as caught:
        uninstall(dep, store, sd)
    assert caught.value.removed == ["mydag.timer", "mydag.service"]
    assert not (store.path / dep.service_name).exists()
    assert not (store.path / dep.timer_name).exists()
    assert not link.is_symlink()
    sd.daemon_reload.assert_called_once()


def test_uninstall_falls_back_to_stop_when_disable_fails(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    sd.disable.side_effect = SystemdError(["systemctl", "disable"], 1, "bus error")
    with pytest.raises(UninstallError, match="bus error"):
        uninstall(dep, store, sd)
    sd.stop.assert_any_call(dep.timer_name, missing_ok=True)
    assert list(store.path.iterdir()) == []


def test_uninstall_rerun_finishes_after_failed_reload(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    link = _install_enabled(dep, store, sd)
    sd.daemon_reload.side_effect = SystemdError(["systemctl", "daemon-reload"], 1, "reload failed")
    loaded = {name: {"LoadState": "loaded", "ActiveState": "active" if name == dep.timer_name else "inactive",
                     "FragmentPath": str(store.path / name)} for name in dep.render_units()}
    sd.show.side_effect = lambda name, *props: loaded[name]
    with pytest.raises(UninstallError, match="still loaded"):
        uninstall(dep, store, sd)
    assert list(store.path.glob("mydag.*")) == []

    # The files are gone but systemd still has them loaded; leave a dangling link too.
    link.symlink_to(store.path / dep.timer_name)
    sd.reset_mock(side_effect=True)
    reloaded = []
    sd.daemon_reload.side_effect = lambda: reloaded.append(True)
    sd.show.side_effect = lambda name, *props: (
        {"LoadState": "not-found", "ActiveState": "inactive", "FragmentPath": ""} if reloaded else loaded[name])
    assert uninstall(dep, store, sd) == []
    sd.stop.assert_any_call(dep.timer_name, missing_ok=True)
    sd.stop.assert_any_call(dep.service_name, missing_ok=True)
    sd.disable.assert_not_called()
    assert reloaded
    assert not link.is_symlink()


def test_uninstall_ignores_same_named_units_loaded_elsewhere(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    sd.show.return_value = {"LoadState": "loaded", "ActiveState": "active",
                            "FragmentPath": "/usr/lib/systemd/user/mydag.service"}
    assert uninstall(dep, store, sd) == []
    assert {c[0] for c in sd.mock_calls} == {"show"}


def test_uninstall_reports_service_still_running(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    sd.show.side_effect = lambda name, *props: {
        "LoadState": "not-found", "FragmentPath": "",
        "ActiveState": "deactivating" if name == dep.service_name else "inactive"}
    with pytest.raises(UninstallError, match="mydag.service is still deactivating"):
        uninstall(dep, store, sd)


def test_cli_uninstall_reports_partial_failure(dep, tmp_path, sd, capsys):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.stop.side_effect = SystemdError(["systemctl", "stop"], 1, "stop timed out")
    assert cli(dep, ["uninstall"], store=store, sd=sd) == 1
    out, err = capsys.readouterr()
    assert "removed: mydag.timer, mydag.service" in out
    assert "stop timed out" in err and "rerun uninstall" in err and "Traceback" not in err


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
