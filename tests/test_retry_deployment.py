"""Environment binding and failed validation must not mutate deployment state."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import floe
from floe.render import exec_line


def test_planning_does_not_inherit_shell_python_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "not-a-python-home"))
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "shell-only-user-packages"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "shell-only-imports"))
    dep = floe.Deployment("demo", tmp_path / "flow.py", ("hourly",))
    store = floe.UnitStore(tmp_path / "units")
    plan = floe.plan_install(dep, store)
    assert len(plan.writes) == 2
    assert not store.path.exists()


def test_interpreter_symlink_is_preserved_in_command_and_metadata(tmp_path, monkeypatch):
    python = tmp_path / "env 100% $money" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(python))
    dep = floe.Deployment("demo", tmp_path / "flow.py", ("hourly",))
    service = dep.render_units()["demo.service"]
    assert dep.python == str(python) and dep.python != str(python.resolve())
    assert f"ExecStart={exec_line([str(python), '-m', 'floe_cli', 'run', str(dep.script)])}\n" in service
    assert f"Python={python}\n" in service


@pytest.mark.parametrize("missing", [False, True])
def test_unusable_python_fails_planning_without_writes_or_manager_calls(tmp_path, missing):
    python = tmp_path / "env" / "bin" / "python"
    if not missing:
        created = subprocess.run(["uv", "venv", "--python", sys.executable, str(tmp_path / "env")],
                                 capture_output=True, text=True, timeout=30)
        assert created.returncode == 0, created.stderr
    dep = floe.Deployment("demo", tmp_path / "flow.py", ("hourly",), python=str(python))
    store = floe.UnitStore(tmp_path / "units")
    sd = create_autospec(floe.Systemd, instance=True)
    with pytest.raises(floe.ValidationError, match="Python environment"):
        floe.install(dep, store, sd)
    assert not store.path.exists()
    assert sd.mock_calls == []


def test_old_and_missing_interpreter_metadata_remain_addressable(tmp_path):
    script = tmp_path / "flow.py"
    script.touch()
    dep = floe.Deployment("demo.py", script, ("hourly",))
    store = floe.UnitStore(tmp_path / "units")
    for name, text in dep.render_units().items():
        store.write(name, text)
    service = store.path / dep.service_name
    original = service.read_text()
    service.write_text(original.replace(f"Python={dep.python}\n", ""))
    found = floe.find_installed(store, "demo.py")
    assert found.script == script
    assert found.problems == ("installed without an interpreter environment; reinstall",)
    service.write_text(original.replace(f"Python={dep.python}", f"Python={tmp_path / 'gone'}"))
    found = floe.find_installed(store, "demo.py")
    assert found.script == script
    assert "interpreter missing" in found.problems[0]
