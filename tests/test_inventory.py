"""Discovering installed workflows from unit files, and summarizing them."""

from pathlib import Path
from unittest.mock import create_autospec

import pytest
from journal_fakes import FakeJournal, Run

from floe.deploy import PACKAGE_ROOT, Deployment, install
from floe.inventory import check_installed, find_installed, list_installed, overview
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
    (store.path / "foreign.service").write_text("[Service]\nExecStart=/bin/true\n")
    (store.path / "masked.timer").symlink_to("/dev/null")
    sd.reset_mock()
    found = list_installed(store)
    assert [(w.name, w.script, w.problems) for w in found] == [("a-flow", second, ()), ("b-flow", first, ())]
    assert found[0].service == store.path / "a-flow.service" and found[0].timer == store.path / "a-flow.timer"
    assert sd.mock_calls == []
    assert list_installed(UnitStore(tmp_path / "absent")) == []


def test_problems_found_without_running_code(tmp_path, store, sd):
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
    assert found.problems == ("installed by an older floe version; reinstall from its definition file",)

    gone = tmp_path / "moved-src"
    for name in ("mydag.service", "mydag.timer"):
        path = store.path / name
        path.write_text(path.read_text() + f"Script={tmp_path / 'mydag.py'}\nFloe={gone}\n")
    [found] = list_installed(store)
    assert found.problems == ("floe has moved since installation; reinstall",)


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
    assert summary.next_run.timestamp() == 1794855600 and summary.last_trigger is None
    assert summary.last_run.run_id == "a" * 32
    sd.show_all.assert_called_once()
    assert sd.show_all.call_args.kwargs == {"unix_timestamps": True}
    data = summary.to_dict()
    assert data["name"] == "mydag" and data["last_run"]["status"] == "succeeded"


def test_overview_reports_inactive_timer_and_errors_per_workflow(tmp_path, store, sd):
    script = deploy(tmp_path, store, sd)
    sd.show_all.return_value = [("UnitFileState", "disabled"), ("ActiveState", "inactive")]
    [summary] = overview(store, sd, FakeJournal([]))
    assert summary.problems == ("timer is inactive",)
    assert summary.last_run is None and summary.next_run is None

    sd.show_all.side_effect = SystemdError(["systemctl"], 1, "no session")
    script.write_text(DEFINITION.format(name="mydag", schedule="daily"))
    [summary] = overview(store, sd, FakeJournal([]), verify=True)
    assert summary.problems[0] == "units out of date (mydag.timer); reinstall"
    assert "no session" in summary.problems[1]


def test_metadata_recorded_in_both_units(tmp_path, store, sd):
    script = deploy(tmp_path, store, sd)
    for name in ("mydag.service", "mydag.timer"):
        assert store.metadata(name) == {"Owner": "floe", "Workflow": "mydag",
                                        "Script": str(script), "Floe": str(PACKAGE_ROOT)}
    assert store.units() == ["mydag.service", "mydag.timer"]
    assert Path(store.metadata("mydag.service")["Floe"], "floe_cli").is_dir()
