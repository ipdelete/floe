from unittest.mock import call, create_autospec

import pytest

from floe.deploy import (
    Deployment,
    apply,
    cancel,
    install,
    plan_install,
    start,
    status,
    uninstall,
    unit_names,
    user_unit_dir,
)
from floe.model import NotFoundError, UninstallError, ValidationError, Workflow
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


@pytest.mark.parametrize("owned_file,local_fragment", [(False, True), (True, False), (True, True)])
def test_cancel_requires_owned_file_and_matching_fragment(tmp_path, sd, monkeypatch, owned_file, local_fragment):
    store = UnitStore(tmp_path)
    service = tmp_path / "mydag.service"
    service.write_text("[X-Floe]\nOwner=floe\n" if owned_file else "[Service]\n")
    run_id = "a" * 32
    sd.show.return_value = {"FragmentPath": str(service if local_fragment else tmp_path / "elsewhere" / service.name),
                            "ActiveState": "activating", "MainPID": "42", "InvocationID": run_id}
    requests = []
    monkeypatch.setattr("floe.deploy.request_cancel", lambda *args: requests.append(args))
    if owned_file and local_fragment:
        assert cancel("mydag", store, sd, task="fetch") == run_id
        assert requests == [(run_id, 42, "fetch")]
    else:
        with pytest.raises(NotOwnedError):
            cancel("mydag", store, sd, task="fetch")
        assert not requests


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


@pytest.mark.parametrize("already_installed", [False, True])
def test_invalid_calendars_never_create_or_replace_units(dep, tmp_path, sd, already_installed):
    store = UnitStore(tmp_path / "units")
    if already_installed:
        install(dep, store, sd)
    before = {name: ((store.path / name).read_bytes(), (store.path / name).stat().st_mtime_ns)
              for name in store.units()}
    sd.reset_mock()
    invalid = Deployment(dep.workflow, dep.script, ("not-a-calendar", "daily", "also-invalid"))
    with pytest.raises(ValidationError) as caught:
        install(invalid, store, sd)
    assert len(caught.value.problems) == 2
    assert "not-a-calendar" in caught.value.problems[0]
    assert "also-invalid" in caught.value.problems[1]
    assert {name: ((store.path / name).read_bytes(), (store.path / name).stat().st_mtime_ns)
            for name in store.units()} == before
    assert sd.mock_calls == []


@pytest.mark.parametrize("already_installed", [False, True])
@pytest.mark.parametrize("timeout", ["not-a-duration", "-1s", "30min nonsense"])
def test_invalid_timeouts_never_create_or_replace_units(dep, tmp_path, sd, already_installed, timeout):
    store = UnitStore(tmp_path / "units")
    if already_installed:
        install(dep, store, sd)
    before = {name: ((store.path / name).read_bytes(), (store.path / name).stat().st_mtime_ns)
              for name in store.units()}
    sd.reset_mock()
    invalid = Deployment(dep.workflow, dep.script, dep.schedule, timeout=timeout)
    with pytest.raises(ValidationError, match="invalid timeout"):
        install(invalid, store, sd)
    assert {name: ((store.path / name).read_bytes(), (store.path / name).stat().st_mtime_ns)
            for name in store.units()} == before
    assert sd.mock_calls == []


@pytest.mark.parametrize("timeout", ["5min", "1h 30min", "0", "infinity"])
def test_timeout_overrides_are_validated_without_rejecting_special_values(dep, tmp_path, timeout):
    deployment = Deployment(dep.workflow, dep.script, dep.schedule, timeout=timeout)
    store = UnitStore(tmp_path / "units")
    plan = plan_install(deployment, store)
    assert f"TimeoutStartSec={timeout}\n" in plan.changes[0].text
    assert not store.path.exists()


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


@pytest.mark.parametrize("schedule_type", [list, tuple, iter])
def test_deployment_schedule_is_normalized_once(dep, schedule_type):
    deployment = Deployment("demo", dep.script, schedule_type(["hourly", "daily"]))
    assert deployment.schedule == ("hourly", "daily")
    assert "OnCalendar=hourly\nOnCalendar=daily\n" in deployment.render_units()["demo.timer"]


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


@pytest.mark.parametrize("service,timer,installed", [
    ("not-found", "not-found", False),
    ("not-found", "loaded", True),   # half an install still has state worth showing
    ("loaded", "not-found", True),
    ("masked", "masked", True),      # masked units exist; status must not hide them
])
def test_status_reports_not_installed_only_when_systemd_knows_neither_unit(sd, service, timer, installed):
    sd.show.side_effect = lambda unit, *props: {"LoadState": service if unit.endswith(".service") else timer}
    if installed:
        assert [state["LoadState"] for state in status("mydag", sd).values()] == [service, timer]
    else:
        with pytest.raises(NotFoundError, match="'mydag' is not installed"):
            status("mydag", sd)


def test_status_query_failure_is_not_reported_as_not_installed(sd):
    sd.show.side_effect = SystemdError(["systemctl", "show"], 1, "cannot connect to bus")
    with pytest.raises(SystemdError):
        status("mydag", sd)


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
        call.clean_state("mydag.timer"),
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


@pytest.mark.parametrize("files_exist", [True, False])
def test_uninstall_retries_failed_timer_state_cleanup(dep, tmp_path, sd, files_exist):
    store = UnitStore(tmp_path / "units")
    link = _install_enabled(dep, store, sd)
    if not files_exist:
        for name in (dep.service_name, dep.timer_name):
            (store.path / name).unlink()
    stamp = tmp_path / "timer-stamp"
    stamp.touch()
    reloaded = []
    sd.daemon_reload.side_effect = lambda: reloaded.append(True)
    sd.show.side_effect = lambda name, *props: (
        {"LoadState": "loaded", "ActiveState": "inactive", "FragmentPath": str(store.path / name)}
        if not reloaded or (store.path / name).exists()
        else {"LoadState": "not-found", "ActiveState": "inactive", "FragmentPath": ""})
    attempts = []

    def clean(name):
        attempts.append(name)
        if len(attempts) == 1:
            raise SystemdError(["systemctl", "clean"], 1, "temporary cleanup failure")
        stamp.unlink()

    sd.clean_state.side_effect = clean
    with pytest.raises(UninstallError, match="temporary cleanup failure") as caught:
        uninstall(dep.workflow, store, sd)
    assert caught.value.removed == ([dep.service_name] if files_exist else [])
    assert store.units() == [dep.timer_name]
    assert store.is_owned(dep.timer_name)
    if not files_exist:
        assert "OnCalendar=1970-01-01 00:00:00 UTC" in (store.path / dep.timer_name).read_text()
        assert store.metadata(dep.timer_name)["Cleanup"] == "pending"
    assert not link.is_symlink()
    assert stamp.exists()

    assert uninstall(dep.workflow, store, sd) == [dep.timer_name]
    assert attempts == [dep.timer_name, dep.timer_name]
    assert not stamp.exists()
    assert store.units() == []


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


def test_uninstall_removes_owned_files_without_stopping_foreign_loaded_units(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    sd.show.side_effect = lambda name, *props: {
        "LoadState": "loaded", "ActiveState": "active",
        "FragmentPath": f"/elsewhere/{name}"}

    assert uninstall(dep.workflow, store, sd) == [dep.timer_name, dep.service_name]
    assert list(store.path.iterdir()) == []
    sd.disable.assert_not_called()
    sd.stop.assert_not_called()
    sd.clean_state.assert_not_called()
    sd.reset_failed.assert_not_called()
    sd.daemon_reload.assert_called_once()


@pytest.mark.parametrize("files_exist", [False, True])
@pytest.mark.parametrize("error", [
    SystemdError(["systemctl", "show"], 1, "cannot connect to bus"),
    FileNotFoundError("systemctl not found"),
])
def test_uninstall_reports_manager_query_failure(dep, tmp_path, sd, files_exist, error):
    store = UnitStore(tmp_path / "units")
    if files_exist:
        install(dep, store, sd)
    sd.reset_mock()
    sd.show.side_effect = error

    with pytest.raises(UninstallError) as caught:
        uninstall(dep.workflow, store, sd)
    assert str(error) in str(caught.value)
    assert caught.value.removed == ([dep.service_name] if files_exist else [])
    assert store.is_owned(dep.timer_name) is files_exist
    sd.disable.assert_not_called()
    sd.stop.assert_not_called()
    sd.clean_state.assert_not_called()


def test_uninstall_reports_service_still_running(dep, tmp_path, sd):
    store = UnitStore(tmp_path / "units")
    install(dep, store, sd)
    sd.reset_mock()
    sd.show.side_effect = lambda name, *props: {
        "LoadState": "not-found", "FragmentPath": "",
        "ActiveState": "deactivating" if name == dep.service_name else "inactive"}
    with pytest.raises(UninstallError, match="mydag.service is still deactivating"):
        uninstall(dep.workflow, store, sd)
