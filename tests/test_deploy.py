import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from floe.deploy import Deployment, cli, install, uninstall
from floe.units import NotOwnedError, ServiceFile, TimerFile, UnitDir, exec_line

ROOT = Path(__file__).parent.parent
LEGACY = Path(__file__).parent / "fixtures" / "legacy"


class FakeSystemd:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.calls.append((name, *a, *sorted(kw.items())))


@pytest.fixture
def dep(tmp_path):
    script = tmp_path / "my flows" / "workflow.py"  # space in path on purpose
    script.parent.mkdir()
    script.touch()
    return Deployment("mydag", script, ["hourly"])


# ----- exec_line ---------------------------------------------------------------
@pytest.mark.parametrize(
    "argv, line",
    [
        (["/usr/bin/python3", "/a/b.py", "run"], "/usr/bin/python3 /a/b.py run"),
        (["/bin/echo", "two words"], '/bin/echo "two words"'),
        (["/bin/echo", 'say "hi"'], r'/bin/echo "say \"hi\""'),
        (["/bin/date", "+%F"], "/bin/date +%%F"),
        (["/bin/echo", "$HOME"], "/bin/echo $$HOME"),
        (["/bin/echo", ";"], r"/bin/echo \;"),
        (["/bin/echo", ""], '/bin/echo ""'),
        (["/bin/echo", "a\\b"], r"/bin/echo a\\b"),
    ],
)
def test_exec_line(argv, line):
    assert exec_line(argv) == line


def test_exec_line_rejects_control_chars():
    with pytest.raises(ValueError):
        exec_line(["/bin/echo", "a\nb"])


# ----- Deployment.units ----------------------------------------------------------
def test_units_generated_from_workflow(dep):
    units = dep.units()
    service, timer = units["mydag.service"], units["mydag.timer"]
    assert isinstance(service, ServiceFile) and isinstance(timer, TimerFile)
    assert service.service.exec_start == [f'/usr/bin/python3 "{dep.script.resolve()}" run']
    assert service.service.working_directory == str(dep.script.parent.resolve())
    assert service.service.timeout_start_sec == "30min"
    assert timer.timer.on_calendar == ["hourly"]
    assert timer.timer.unit == "mydag.service"
    assert service.x_floe.workflow == timer.x_floe.workflow == "mydag"


def test_generated_units_cover_legacy_units(dep):
    """Everything the hand-written units set is still set (values may differ)."""
    keys = lambda raw: {(s, k) for s, pairs in raw.items() for k, _ in pairs if s != "X-Floe"}
    legacy = UnitDir(LEGACY, owner=None)
    for name, unit in dep.units().items():
        assert keys(legacy[name].to_raw()) <= keys(unit.to_raw())


def test_generated_units_pass_systemd_analyze(dep, tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    units = UnitDir(tmp_path / "units")
    for name, unit in dep.units().items():
        units[name] = unit
    r = subprocess.run(
        ["systemd-analyze", "--user", "verify", *(str(units.path / n) for n in units)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


# ----- install / uninstall ----------------------------------------------------------
def test_install_then_reinstall_is_idempotent(dep, tmp_path):
    units, sd = UnitDir(tmp_path / "units"), FakeSystemd()
    assert install(dep, units, sd) == ["mydag.service", "mydag.timer"]
    assert sd.calls == [("daemon_reload",), ("enable", "mydag.timer", ("now", True))]

    sd.calls.clear()
    assert install(dep, units, sd) == []
    assert sd.calls == [("enable", "mydag.timer", ("now", True))]  # no reload when nothing changed


def test_install_refuses_foreign_units_without_adopt(dep, tmp_path):
    units, sd = UnitDir(tmp_path / "units"), FakeSystemd()
    units.path.mkdir()
    shutil.copy(LEGACY / "mydag.timer", units.path)
    with pytest.raises(NotOwnedError, match="--adopt"):
        install(dep, units, sd)
    assert not (units.path / "mydag.service").exists()  # nothing written
    assert sd.calls == []

    assert install(dep, units, sd, adopt=True) == ["mydag.service", "mydag.timer"]
    assert "mydag.timer" in units


def test_uninstall(dep, tmp_path):
    units, sd = UnitDir(tmp_path / "units"), FakeSystemd()
    install(dep, units, sd)
    sd.calls.clear()
    assert uninstall(dep, units, sd) == ["mydag.timer", "mydag.service"]
    assert sd.calls == [
        ("disable", "mydag.timer", ("now", True)),
        ("stop", "mydag.service"),
        ("daemon_reload",),
    ]
    assert list(units) == []
    sd.calls.clear()
    assert uninstall(dep, units, sd) == [] and sd.calls == []


def test_cli_dry_run_prints_exact_files(dep, tmp_path, capsys):
    units = UnitDir(tmp_path / "units")
    assert cli(dep, ["install", "--dry-run"], units=units, sd=FakeSystemd()) == 0
    out = capsys.readouterr().out
    assert "OnCalendar=hourly" in out and "# Managed by floe." in out
    assert not units.path.exists()


def test_cli_reports_errors(dep, tmp_path, capsys):
    units = UnitDir(tmp_path / "units")
    units.path.mkdir()
    shutil.copy(LEGACY / "mydag.service", units.path)
    assert cli(dep, ["install"], units=units, sd=FakeSystemd()) == 1
    assert "--adopt" in capsys.readouterr().err


# ----- workflow.py -------------------------------------------------------------------
def test_workflow_runs_with_system_python_without_floe():
    """systemd runs `python3 workflow.py run` outside the venv: must not need pydantic."""
    r = subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "workflow.py"), "run"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert '"event": "run_completed"' in r.stdout


def test_workflow_deploy_commands_explain_missing_floe():
    r = subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "workflow.py"), "status"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 1 and "uv run" in r.stderr


def test_workflow_dry_run_via_venv():
    r = subprocess.run([sys.executable, str(ROOT / "workflow.py"), "install", "--dry-run"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert f"ExecStart=/usr/bin/python3 {ROOT / 'workflow.py'} run" in r.stdout


def test_cli_uses_given_empty_unitdir(dep, tmp_path):
    """Regression: an empty UnitDir is falsy; cli must still use it, not the default."""
    units = UnitDir(tmp_path / "units")
    assert len(units) == 0
    assert cli(dep, ["install"], units=units, sd=FakeSystemd()) == 0
    assert list(units) == ["mydag.service", "mydag.timer"]
    assert not (tmp_path / "default-unit-dir-must-not-be-used").exists()
