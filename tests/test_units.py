import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from floe.units import (
    InstallSection,
    NotOwnedError,
    ServiceFile,
    ServiceSection,
    TimerFile,
    TimerSection,
    UnitDir,
    UnitSection,
    UnitSyntaxError,
    parse,
    serialize,
)

REPO_UNITS = Path(__file__).parent / "fixtures" / "legacy"


@pytest.fixture
def unitdir(tmp_path):
    """A copy of the repo's unit files, laid out like an installed, enabled user dir."""
    for f in REPO_UNITS.iterdir():
        shutil.copy(f, tmp_path)
    wants = tmp_path / "timers.target.wants"
    wants.mkdir()
    (wants / "mydag.timer").symlink_to(tmp_path / "mydag.timer")
    return tmp_path


def sync_service(**service):
    return ServiceFile(
        unit=UnitSection(description="Daily Sync"),
        service=ServiceSection(type="oneshot", exec_start=["/usr/bin/python3 runner.py"], **service),
    )


# ----- format layer ----------------------------------------------------------
def test_parse_comments_continuations_repeats():
    raw = parse(
        "# c\n; c\n[Service]\nExecStart=/bin/a \\\n  --flag\nExecStart = /bin/b\n\n[Unit]\nX=1\n"
    )
    # syntax(7): "the backslash is replaced by a space character"
    assert raw == {"Service": [("ExecStart", "/bin/a  --flag"), ("ExecStart", "/bin/b")], "Unit": [("X", "1")]}


def test_parse_syntax7_example():
    # Verbatim example from systemd.syntax(7).
    raw = parse(
        "[Section A]\nKeyOne=value 1\nKeyTwo=value 2\n\n# a comment\n\n"
        "[Section B]\nSetting=\"something\" \"some thing\" \"...\"\nKeyTwo=value 2 \\\n       value 2 continued\n\n"
        "[Section C]\nKeyThree=value 3\\\n# this line is ignored\n; this line is ignored too\n       value 3 continued\n"
    )
    assert raw == {
        "Section A": [("KeyOne", "value 1"), ("KeyTwo", "value 2")],
        "Section B": [("Setting", '"something" "some thing" "..."'), ("KeyTwo", "value 2  value 2 continued")],
        "Section C": [("KeyThree", "value 3 value 3 continued")],
    }


def test_parse_rejects_garbage():
    with pytest.raises(UnitSyntaxError):
        parse("Key=outside\n")
    with pytest.raises(UnitSyntaxError):
        parse("[Unit]\nnot an assignment\n")


# ----- typed layer -----------------------------------------------------------
@pytest.mark.parametrize("name", ["mydag.service", "mydag.timer"])
def test_repo_files_round_trip(name):
    text = (REPO_UNITS / name).read_text()
    unit = UnitDir(REPO_UNITS, owner=None)[name]
    canonical = serialize(unit.to_raw())
    model = type(unit)
    # model -> text -> model is lossless
    assert model.from_raw(parse(canonical)) == unit
    # canonical text is a fixed point (deterministic output)
    assert serialize(model.from_raw(parse(canonical)).to_raw()) == canonical
    # same sections and keys as the hand-written file; order and spelling
    # (e.g. Persistent=true -> yes) may be normalised
    keys = lambda t: sorted((s, k) for s, pairs in parse(t).items() for k, _ in pairs)
    assert keys(canonical) == keys(text)


def test_repo_files_typed_values():
    units = UnitDir(REPO_UNITS, owner=None)
    timer = units["mydag.timer"]
    assert isinstance(timer, TimerFile)
    assert timer.timer.persistent is True
    assert timer.timer.on_calendar == ["hourly"]
    assert timer.install.wanted_by == ["timers.target"]
    service = units["mydag.service"]
    assert service.service.exec_start == ["/usr/bin/python3 %h/src/floe/workflow.py"]


def test_empty_assignment_resets_list():
    unit = TimerFile.from_raw(parse("[Timer]\nOnCalendar=daily\nOnCalendar=\nOnCalendar=hourly\n"))
    assert unit.timer.on_calendar == ["hourly"]
    unit = ServiceFile.from_raw(parse("[Service]\nType=oneshot\nExecStart=/a\nExecStart=\nExecStart=/b\n"))
    assert unit.service.exec_start == ["/b"]


def test_empty_timer_trigger_resets_all_triggers():
    # systemd.timer(5): resets "both monotonic timers and OnCalendar= timers"
    text = "[Timer]\nOnCalendar=daily\nOnBootSec=5min\nOnActiveSec=1h\nOnBootSec=\nOnUnitActiveSec=1d\n"
    timer = TimerFile.from_raw(parse(text)).timer
    assert (timer.on_calendar, timer.on_boot_sec, timer.on_active_sec, timer.on_unit_active_sec) == ([], [], [], ["1d"])


def test_no_reset_where_undocumented():
    # ExecStop= documents no reset: values are kept verbatim.
    unit = ServiceFile.from_raw(parse("[Service]\nExecStop=/a\nExecStop=\nExecStop=/b\n"))
    assert unit.service.exec_stop == ["/a", "", "/b"]


def test_space_separated_lists():
    unit = TimerFile.from_raw(parse(
        "[Unit]\nAfter=a.service b.service\nAfter=c.service\n[Timer]\n[Install]\nWantedBy=x.target  y.target\n"
    ))
    assert unit.unit.after == ["a.service", "b.service", "c.service"]
    assert unit.install.wanted_by == ["x.target", "y.target"]
    assert ("After", "a.service b.service c.service") in unit.to_raw()["Unit"]
    with pytest.raises(ValidationError, match="space-separated"):
        UnitSection(after=["a.service b.service"])


def test_multiple_exec_start_requires_oneshot():
    # systemd.service(5): "Unless Type= is oneshot, exactly one command must be given."
    with pytest.raises(ValidationError, match="oneshot"):
        ServiceSection(exec_start=["/a", "/b"])
    with pytest.raises(ValidationError, match="oneshot"):
        ServiceSection(type="simple", exec_start=["/a", "/b"])
    ServiceSection(type="oneshot", exec_start=["/a", "/b"])


def test_timer_unit_must_not_be_timer():
    with pytest.raises(ValidationError, match="suffix"):
        TimerSection(unit="x.timer")


@pytest.mark.parametrize("bad", ["two\nlines", " padded", "ends with \\", "nul\x00"])
def test_values_that_would_not_read_back(bad):
    with pytest.raises(ValidationError):
        UnitSection(description=bad)


def test_empty_item_in_resettable_list_rejected():
    with pytest.raises(ValidationError, match="reset"):
        TimerSection(on_calendar=["daily", ""])


def test_scalar_last_assignment_wins():
    unit = ServiceFile.from_raw(parse("[Service]\nType=simple\nType=oneshot\n"))
    assert unit.service.type == "oneshot"


def test_unknown_keys_and_sections_survive_round_trip():
    text = "[Unit]\nDescription=x\n\n[Service]\nExecStart=/bin/true\nNice=5\nFoo=a\nFoo=b\n\n[X-Other]\nK=v\n"
    unit = ServiceFile.from_raw(parse(text))
    assert unit.service.model_extra == {"Nice": "5", "Foo": ["a", "b"]}
    assert serialize(unit.to_raw()) == text


def test_bool_spellings():
    for spelling, expected in [("yes", True), ("on", True), ("1", True), ("no", False), ("false", False)]:
        unit = TimerFile.from_raw(parse(f"[Timer]\nPersistent={spelling}\n"))
        assert unit.timer.persistent is expected
    assert ("Persistent", "no") in TimerFile(timer=TimerSection(persistent=False)).to_raw()["Timer"]


def test_percent_must_be_escaped():
    with pytest.raises(ValidationError, match="literal"):
        ServiceSection(exec_start=["/bin/date +%F"])
    ServiceSection(type="oneshot", exec_start=["/bin/date +%%F", "%h/bin/x", "%Y/x"])  # escaped and specifier: fine


def test_models_are_frozen():
    unit = sync_service()
    with pytest.raises(ValidationError):
        unit.service.type = "simple"


def test_invalid_enum_rejected():
    with pytest.raises(ValidationError):
        ServiceSection(type="sometimes")


# ----- CRUD layer ------------------------------------------------------------
def test_unowned_files_are_invisible_and_protected(unitdir):
    units = UnitDir(unitdir)
    assert list(units) == []
    assert "mydag.service" not in units
    with pytest.raises(KeyError):
        units["mydag.service"]
    with pytest.raises(NotOwnedError):
        units["mydag.service"] = sync_service()
    with pytest.raises(NotOwnedError):
        del units["mydag.timer"]
    assert UnitDir(unitdir, owner=None)["mydag.service"].unit.description == "My DAG"


def test_listing_skips_symlinks_and_dirs(unitdir):
    assert list(UnitDir(unitdir, owner=None)) == ["mydag.service", "mydag.timer"]


def test_create_read_update_delete(unitdir):
    units = UnitDir(unitdir)
    units["dag-sync.service"] = sync_service()
    assert list(units) == ["dag-sync.service"]
    assert len(units) == 1

    got = units["dag-sync.service"]
    assert got.x_floe.owner == "floe"
    assert got.service.exec_start == ["/usr/bin/python3 runner.py"]
    assert (unitdir / "dag-sync.service").read_text().startswith("# Managed by floe.")

    units["dag-sync.service"] = got.model_copy(
        update={"service": got.service.model_copy(update={"timeout_start_sec": "30min"})}
    )
    assert units["dag-sync.service"].service.timeout_start_sec == "30min"

    del units["dag-sync.service"]
    assert "dag-sync.service" not in units
    assert not (unitdir / "dag-sync.service").exists()


def test_put_is_idempotent(unitdir):
    units = UnitDir(unitdir)
    assert units.put("dag-sync.service", sync_service()) is True
    mtime = (unitdir / "dag-sync.service").stat().st_mtime_ns
    assert units.put("dag-sync.service", units["dag-sync.service"]) is False
    assert (unitdir / "dag-sync.service").stat().st_mtime_ns == mtime


def test_force_takes_over_foreign_file(unitdir):
    units = UnitDir(unitdir)
    foreign = UnitDir(unitdir, owner=None)["mydag.timer"]
    units.put("mydag.timer", foreign, force=True)
    assert "mydag.timer" in units
    assert units["mydag.timer"].timer.on_calendar == ["hourly"]


def test_delete_removes_enable_symlinks(unitdir):
    units = UnitDir(unitdir)
    units.put("mydag.timer", UnitDir(unitdir, owner=None)["mydag.timer"], force=True)
    assert (unitdir / "timers.target.wants" / "mydag.timer").is_symlink()
    del units["mydag.timer"]
    assert not (unitdir / "timers.target.wants" / "mydag.timer").exists(follow_symlinks=False)


def test_wrong_model_for_suffix(unitdir):
    with pytest.raises(TypeError):
        UnitDir(unitdir)["dag-sync.timer"] = sync_service()


@pytest.mark.parametrize("name", ["x.conf", "../x.service", "dag@daily.service", ".hidden.service"])
def test_bad_names(unitdir, name):
    units = UnitDir(unitdir)
    assert name not in units
    with pytest.raises(KeyError):
        units[name]


def test_template_file_allowed(unitdir):
    units = UnitDir(unitdir)
    units["dag@.service"] = sync_service()
    assert "dag@.service" in units


def test_masked_unit_not_overwritten(unitdir):
    (unitdir / "masked.service").symlink_to("/dev/null")
    with pytest.raises(NotOwnedError):
        UnitDir(unitdir).put("masked.service", sync_service(), force=True)


def test_generated_files_pass_systemd_analyze_verify(tmp_path):
    analyze = shutil.which("systemd-analyze")
    if not analyze:
        pytest.skip("systemd-analyze not available")
    import subprocess

    units = UnitDir(tmp_path)
    units["dag-sync.service"] = sync_service(timeout_start_sec="30min", kill_mode="control-group")
    units["dag-sync.timer"] = TimerFile(
        unit=UnitSection(description="Schedule Daily Sync"),
        timer=TimerSection(on_calendar=["*-*-* 08:00:00"], persistent=True, unit="dag-sync.service"),
        install=InstallSection(wanted_by=["timers.target"]),
    )
    r = subprocess.run(
        [analyze, "--user", "verify", *(str(tmp_path / n) for n in units)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
