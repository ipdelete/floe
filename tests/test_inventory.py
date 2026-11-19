"""Discovering installed workflows from unit files, and summarizing them."""

import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from journal_fakes import FakeJournal, Run

from floe.deploy import PACKAGE_ROOT, Deployment, install
from floe.inventory import check_installed, find_installed, list_installed, overview
from floe.journal import Journal
from floe.model import NotFoundError, SystemdError
from floe.store import UnitStore
from floe.systemd import Systemd

DEFINITION = 'WORKFLOW = "{name}"\nSCHEDULE = ["{schedule}"]\ntasks = {{"a": print}}\ndag = {{"a": set()}}\n'


@pytest.fixture
def sd():
    sd = create_autospec(Systemd, instance=True)
    sd.show.return_value = {"ActiveState": "inactive", "InvocationID": ""}
    sd.show_all.return_value = [
        ("UnitFileState", "enabled"), ("ActiveState", "active"),
        ("TimersCalendar", "{ OnCalendar=*-*-* *:00:00 ; next_elapse=@1794855600 }"),
        ("TimersCalendar", "{ OnCalendar=*-*-* 08:00:00 ; next_elapse=@1794920400 }"),
        ("NextElapseUSecRealtime", "@1794855600"), ("LastTriggerUSec", ""),
    ]
    return sd


@pytest.fixture
def store(tmp_path):
    return UnitStore(tmp_path / "units")


def deploy(tmp_path, store, sd, name="mydag", schedule="hourly"):
    script = tmp_path / f"{name}.py"
    script.write_text(DEFINITION.format(name=name, schedule=schedule))
    install(Deployment(name, script, [schedule]), store, sd)
    return script


def test_list_installed_reads_metadata_only(tmp_path, store, sd):
    first = deploy(tmp_path, store, sd, "b-flow")
    second = deploy(tmp_path, store, sd, "a-flow")
    executed = tmp_path / "definition-executed"
    for script in (first, second):
        script.write_text(
            f'from pathlib import Path\nPath({str(executed)!r}).touch()\n'
            'raise RuntimeError("listing must not execute definitions")\n'
        )
    (store.path / "foreign.service").write_text("[Service]\nExecStart=/bin/true\n")
    (store.path / "masked.timer").symlink_to("/dev/null")
    sd.reset_mock()
    found = list_installed(store)
    assert [(w.name, w.script, w.problems) for w in found] == [("a-flow", second, ()), ("b-flow", first, ())]
    assert found[0].service == store.path / "a-flow.service" and found[0].timer == store.path / "a-flow.timer"
    assert sd.mock_calls == []
    assert not executed.exists()
    assert list_installed(UnitStore(tmp_path / "absent")) == []


def test_missing_unit_and_script_are_reported(tmp_path, store, sd):
    script = deploy(tmp_path, store, sd)
    (store.path / "mydag.timer").unlink()
    script.unlink()
    [found] = list_installed(store)
    assert found.problems == ("timer unit is missing; reinstall", f"script not found: {script}")


def test_moved_floe_and_old_units(tmp_path, store, sd):
    deploy(tmp_path, store, sd)
    [found] = list_installed(store, package_root=tmp_path / "elsewhere")
    assert found.problems == (f"uses floe from {PACKAGE_ROOT}; reinstall to use {tmp_path / 'elsewhere'}",)

    for name in ("mydag.service", "mydag.timer"):  # as written before Script=/Floe= existed
        path = store.path / name
        path.write_text(path.read_text().split("Script=")[0])
    [found] = list_installed(store)
    assert found.script is None
    assert found.problems == ("installed without an interpreter environment; reinstall",
                              "installed by an older floe version; reinstall from its definition file")

    gone = tmp_path / "moved-src"
    for name in ("mydag.service", "mydag.timer"):
        path = store.path / name
        path.write_text(path.read_text() + f"Script={tmp_path / 'mydag.py'}\nFloe={gone}\n")
    [found] = list_installed(store)
    assert found.problems == ("installed without an interpreter environment; reinstall",
                              "floe has moved since installation; reinstall")


def test_cleanup_marker_reports_recovery_instead_of_an_old_installation(store):
    store.write("review.py.timer", "[Timer]\nOnCalendar=1970-01-01 00:00:00 UTC\n\n"
                "[X-Floe]\nOwner=floe\nWorkflow=review.py\nCleanup=pending\n")
    [found] = list_installed(store)
    assert found.script is None
    assert found.problems == ("timer-state cleanup pending; run floe uninstall --name review.py",)
    assert check_installed(found, store) == []


def test_find_installed(tmp_path, store, sd):
    script = deploy(tmp_path, store, sd)
    assert find_installed(store, "mydag").script == script
    with pytest.raises(NotFoundError, match="not installed"):
        find_installed(store, "other")


def test_check_installed_detects_drift(tmp_path, store, sd):
    script = deploy(tmp_path, store, sd)
    [found] = list_installed(store)
    assert check_installed(found, store) == []
    script.write_text(DEFINITION.format(name="mydag", schedule="daily"))
    assert check_installed(found, store) == ["units out of date (mydag.timer); reinstall"]
    script.write_text(DEFINITION.format(name="renamed", schedule="daily"))
    assert "now names workflow 'renamed'" in check_installed(found, store)[0]
    script.write_text("raise RuntimeError('boom')")
    assert check_installed(found, store)[0].startswith("definition does not load:")


def test_overview(tmp_path, store, sd):
    deploy(tmp_path, store, sd)
    journal = FakeJournal(Run("a" * 32).succeeded_run().records)
    [summary] = overview(store, sd, journal)
    assert summary.name == "mydag"
    assert summary.schedule == ("*-*-* *:00:00", "*-*-* 08:00:00")
    assert (summary.enabled, summary.active, summary.problems) == (True, True, ())
    assert summary.timer_state.name == "mydag.timer"
    assert summary.timer_state["UnitFileState"] == "enabled"
    assert summary.timer_state["ActiveState"] == "active"
    assert summary.next_run.timestamp() == 1794855600 and summary.last_trigger is None
    assert summary.last_run.run_id == "a" * 32
    sd.show_all.assert_called_once()
    assert sd.show_all.call_args.kwargs == {"unix_timestamps": True}
    data = summary.to_dict()
    assert data["name"] == "mydag" and data["last_run"]["status"] == "succeeded"
    assert data["timer_state"] == summary.timer_state.to_dict()


@pytest.mark.parametrize("failure", ["systemd", "journal"])
def test_overview_reports_inactive_timer_and_errors_per_workflow(tmp_path, store, sd, failure):
    script = deploy(tmp_path, store, sd)
    active_props = list(sd.show_all.return_value)
    sd.show_all.return_value = [("UnitFileState", "disabled"), ("ActiveState", "inactive")]
    [summary] = overview(store, sd, FakeJournal([]))
    assert summary.problems == ("timer is inactive",)
    assert summary.last_run is None and summary.next_run is None
    assert summary.timer_state["ActiveState"] == "inactive"
    assert summary.timer_state["UnitFileState"] == "disabled"

    deploy(tmp_path, store, sd, name="z-healthy")
    script.write_text(DEFINITION.format(name="mydag", schedule="daily"))

    def show_timer(unit, *props, **kwargs):
        if failure == "systemd" and unit == "mydag.timer":
            raise SystemdError(["systemctl"], 1, "no session")
        return active_props

    last_run = Run("a" * 32).succeeded_run()
    history = FakeJournal(last_run.records)

    def records(unit, **kwargs):
        if failure == "journal" and unit == "mydag.service":
            raise SystemdError(["journalctl"], 1, "journal unavailable")
        return history.records(unit, **kwargs)

    sd.show_all.side_effect = show_timer
    journal = create_autospec(Journal, instance=True)
    journal.records.side_effect = records
    summaries = overview(store, sd, journal, verify=True)
    assert [summary.name for summary in summaries] == ["mydag", "z-healthy"]
    broken, healthy = summaries
    assert broken.problems[0] == "units out of date (mydag.timer); reinstall"
    assert ("no session" if failure == "systemd" else "journal unavailable") in broken.problems[1]
    if failure == "systemd":
        assert broken.last_run.run_id == last_run.id
        assert broken.timer_state is None
        assert broken.to_dict()["timer_state"] is None
    else:
        assert broken.timer_state["ActiveState"] == "active"
        assert broken.timer_state["UnitFileState"] == "enabled"
    assert healthy.problems == ()
    assert healthy.enabled and healthy.active
    assert healthy.schedule == ("*-*-* *:00:00", "*-*-* 08:00:00")
    assert healthy.next_run.timestamp() == 1794855600
    assert healthy.last_run is None
    assert journal.records.call_args.args == ("z-healthy.service",)


def test_metadata_recorded_in_both_units(tmp_path, store, sd):
    script = deploy(tmp_path, store, sd)
    for name in ("mydag.service", "mydag.timer"):
        assert store.metadata(name) == {"Owner": "floe", "Workflow": "mydag",
                                        "Script": str(script), "Floe": str(PACKAGE_ROOT),
                                        **({"Python": sys.executable} if name.endswith(".service") else {})}
    assert store.units() == ["mydag.service", "mydag.timer"]
    assert Path(store.metadata("mydag.service")["Floe"], "floe_cli").is_dir()
