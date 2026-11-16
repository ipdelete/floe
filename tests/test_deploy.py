from unittest.mock import call, create_autospec

import pytest

from floe.deploy import (
    Deployment,
    apply,
    install,
    plan_install,
    start,
    status,
    uninstall,
    unit_names,
    user_unit_dir,
)
from floe.model import UninstallError, ValidationError, Workflow
from floe.store import NotOwnedError, UnitStore
from floe.systemd import Systemd, SystemdError


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
    with pytest.raises(NotOwnedError, match="adopt to take over"):
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


def test_plan_describes_changes_without_writing(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    plan = plan_install(dep, store)
    assert [(c.name, c.action) for c in plan.changes] == [("mydag.service", "create"), ("mydag.timer", "create")]
    assert not store.path.exists()
    apply(plan, store, sd)
    assert {c.action for c in plan_install(dep, store).changes} == {"unchanged"}

    changed = Deployment(dep.workflow, dep.script, ["daily"])
    plan = plan_install(changed, store)
    assert [(c.name, c.action) for c in plan.changes] == [("mydag.service", "unchanged"), ("mydag.timer", "update")]
    timer = plan.changes[1]
    assert "-OnCalendar=hourly" in timer.diff() and "+OnCalendar=daily" in timer.diff()
    assert plan.to_dict()["changes"][1]["action"] == "update"


def test_plan_reports_conflicts_and_apply_refuses_them(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    store.path.mkdir()
    (store.path / dep.timer_name).write_text("[Timer]\nOnCalendar=daily\n")
    (store.path / dep.service_name).symlink_to("/dev/null")
    plan = plan_install(dep, store)
    assert [(c.action, c.reason) for c in plan.changes] == [
        ("conflict", "not a regular file (masked or linked?)"),
        ("conflict", "not managed by floe (adopt to take over)"),
    ]
    with pytest.raises(NotOwnedError):
        apply(plan, store, sd)
    assert sd.mock_calls == []
    adopt = plan_install(dep, store, adopt=True)
    assert [c.action for c in adopt.changes] == ["conflict", "adopt"]


def test_apply_rechecks_ownership_at_write_time(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    plan = plan_install(dep, store)
    store.path.mkdir()
    (store.path / dep.service_name).write_text("foreign, written after planning")
    with pytest.raises(NotOwnedError):
        apply(plan, store, sd)
    assert (store.path / dep.service_name).read_text() == "foreign, written after planning"


def test_deployment_for_workflow(dep, tmp_path):
    workflow = Workflow("demo", {}, {}, ["daily"], source=dep.script)
    deployment = Deployment.for_workflow(workflow, timeout="5min")
    assert (deployment.workflow, deployment.script, deployment.schedule, deployment.timeout) == (
        "demo", dep.script, ("daily",), "5min")
    with pytest.raises(ValidationError, match="no source"):
        Deployment.for_workflow(Workflow("demo", {}, {}, ["daily"]))
    with pytest.raises(ValidationError, match="at least one"):
        Deployment.for_workflow(Workflow("demo", {}, {}, source=dep.script))


def test_start_and_status_use_unit_names(sd):
    start("mydag", sd)
    sd.start.assert_called_once_with("mydag.service", wait=True)
    sd.show.return_value = {"ActiveState": "inactive"}
    result = status("mydag", sd)
    assert list(result) == ["mydag.service", "mydag.timer"]
    assert result["mydag.service"]["ActiveState"] == "inactive"
    assert result["mydag.timer"].to_dict()["name"] == "mydag.timer"
    with pytest.raises(ValidationError):
        unit_names("../bad")


def test_user_unit_dir_resolved_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom"))
    assert user_unit_dir() == tmp_path / "custom" / "systemd" / "user"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert user_unit_dir() == tmp_path / "home" / ".config" / "systemd" / "user"


def test_render_failure_writes_nothing(dep, tmp_path, sd):
    dep = Deployment(dep.workflow, dep.script, dep.schedule, timeout="bad\nvalue")
    store = UnitStore(tmp_path / "units")
    with pytest.raises(ValidationError):
        install(dep, store, sd)
    assert not store.path.exists()
    assert sd.mock_calls == []


def test_uninstall(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    assert uninstall(dep.workflow, store, sd) == ["mydag.timer", "mydag.service"]
    assert [c for c in sd.mock_calls if c[0] != "show"] == [
        call.disable("mydag.timer", now=True),
        call.stop("mydag.service", missing_ok=True),
        call.daemon_reload(),
    ]
    assert list(store.path.iterdir()) == []
    sd.reset_mock()
    assert uninstall(dep.workflow, store, sd) == []
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
        uninstall(dep.workflow, store, sd)
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
        uninstall(dep.workflow, store, sd)
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
        uninstall(dep.workflow, store, sd)
    assert list(store.path.glob("mydag.*")) == []

    # The files are gone but systemd still has them loaded; leave a dangling link too.
    link.symlink_to(store.path / dep.timer_name)
    sd.reset_mock(side_effect=True)
    reloaded = []
    sd.daemon_reload.side_effect = lambda: reloaded.append(True)
    sd.show.side_effect = lambda name, *props: (
        {"LoadState": "not-found", "ActiveState": "inactive", "FragmentPath": ""} if reloaded else loaded[name])
    assert uninstall(dep.workflow, store, sd) == []
    sd.stop.assert_any_call(dep.timer_name, missing_ok=True)
    sd.stop.assert_any_call(dep.service_name, missing_ok=True)
    sd.disable.assert_not_called()
    assert reloaded
    assert not link.is_symlink()


def test_uninstall_resets_failed_units_so_systemd_forgets_them(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    failed = {dep.service_name}  # the last run failed: the unit lingers after removal
    sd.reset_failed.side_effect = failed.discard
    sd.show.side_effect = lambda name, *props: {
        "LoadState": "not-found", "FragmentPath": "",
        "ActiveState": "failed" if name in failed else "inactive"}
    assert uninstall(dep.workflow, store, sd) == ["mydag.timer", "mydag.service"]
    sd.reset_failed.assert_called_once_with(dep.service_name)


def test_uninstall_reports_failed_state_that_could_not_be_reset(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_failed.side_effect = SystemdError(["systemctl", "reset-failed"], 1, "bus error")
    sd.show.side_effect = lambda name, *props: {
        "LoadState": "not-found", "FragmentPath": "",
        "ActiveState": "failed" if name == dep.service_name else "inactive"}
    with pytest.raises(UninstallError, match="mydag.service is still failed"):
        uninstall(dep.workflow, store, sd)


def test_uninstall_ignores_same_named_units_loaded_elsewhere(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    sd.show.return_value = {"LoadState": "loaded", "ActiveState": "active",
                            "FragmentPath": "/usr/lib/systemd/user/mydag.service"}
    assert uninstall(dep.workflow, store, sd) == []
    assert {c[0] for c in sd.mock_calls} == {"show"}


def test_uninstall_reports_service_still_running(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    sd.show.side_effect = lambda name, *props: {
        "LoadState": "not-found", "FragmentPath": "",
        "ActiveState": "deactivating" if name == dep.service_name else "inactive"}
    with pytest.raises(UninstallError, match="mydag.service is still deactivating"):
        uninstall(dep.workflow, store, sd)
