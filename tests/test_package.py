import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).parents[1]


def test_approved_runtime_dependencies_and_floe_command():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]
    assert project["dependencies"] == ["tenacity>=9.1.4,<10"]
    assert project["scripts"] == {"floe": "floe_cli.main:main"}
    assert pyproject["tool"]["uv"]["build-backend"]["module-name"] == ["floe", "floe_cli", "floe_web"]


def test_wheel_installs_with_icons_and_console_entry_point_outside_checkout(tmp_path):
    built = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path / "dist"), str(ROOT)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    [wheel] = (tmp_path / "dist").glob("*.whl")
    with ZipFile(wheel) as archive:
        expected = ROOT / "src" / "floe_web" / "icons"
        assert {name for name in archive.namelist() if name.endswith(".svg")} == {
            "floe_web/logo.svg", *(f"floe_web/icons/{path.name}" for path in expected.glob("*.svg"))}
        assert archive.read("floe_web/logo.svg") == (ROOT / "src/floe_web/logo.svg").read_bytes()
        for path in expected.iterdir():
            assert archive.read(f"floe_web/icons/{path.name}") == path.read_bytes()

    environment = tmp_path / "venv"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    python = environment / "bin" / "python"
    installed = subprocess.run(
        ["uv", "pip", "install", "--offline", "--python", str(python), str(wheel)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    rendered = subprocess.run(
        [str(python), "-I", "-c", r'''
import sys
import os
import json
import shlex
import subprocess
from pathlib import Path
from importlib.resources import files
from xml.etree import ElementTree
import floe
import floe_cli
import floe_web
from floe_web import render
from floe_web.assets import ICON_NAMES

for module in (floe, floe_cli, floe_web):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
for name in ICON_NAMES:
    svg = ElementTree.fromstring(render.icon(name))
    assert svg.get("aria-hidden") == "true"
    assert svg.get("stroke") == "currentColor"
license = files("floe_web").joinpath("icons", "LICENSE").read_text()
assert "ISC License" in license and "The MIT License (MIT)" in license
page = render.document("Workflows", render.inventory([]), "/")
assert b"No installed workflows" in page and b"lucide-refresh-cw" in page
assert b"floe-mark" in page and b'href="/favicon.svg"' in page
brand = ElementTree.fromstring(render.brand_link())
assert brand[0].get("aria-hidden") == "true" and brand[0].tail == "Floe"
favicon = ElementTree.fromstring(render.favicon())
assert len([node for node in favicon if node.tag.endswith("}path")]) == 2
script = Path.cwd() / "retry_flow.py"
script.write_text('from floe import RetryPolicy\nWORKFLOW = "packaged"\nSCHEDULE = ["hourly"]\ncount = 0\ndef task():\n    global count\n    count += 1\n    if count == 1: raise TimeoutError("transient")\ntasks = {"task": task}\ndag = {"task": set()}\nretries = {"task": RetryPolicy(2, 0, (TimeoutError,))}\n')
dep = floe.Deployment.for_workflow(floe.load(script))
assert dep.python == sys.executable
units = dep.render_units()
service = units["packaged.service"]
assert f"Python={sys.executable}\n" in service
command = next(line.removeprefix("ExecStart=") for line in service.splitlines() if line.startswith("ExecStart="))
# Test paths contain no systemd specifiers or dollar expansions.
argv = shlex.split(command)
assert argv == [sys.executable, "-m", "floe_cli", "run", str(script)]
env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(dep.pythonpath)}
result = subprocess.run(argv, env=env, cwd=script.parent, capture_output=True, text=True, timeout=10)
assert result.returncode == 0, result.stderr
attempts = [json.loads(line) for line in result.stdout.splitlines() if json.loads(line)["event"] == "attempt_completed"]
assert [event["status"] for event in attempts] == ["failed", "succeeded"]
floe.plan_install(dep, floe.UnitStore(Path.cwd() / "units"))
assert not (Path.cwd() / "units").exists()
paths = []
for name, contents in units.items():
    path = Path.cwd() / name
    path.write_text(contents)
    paths.append(str(path))
verified = subprocess.run(["systemd-analyze", "--user", "verify", *paths], capture_output=True, text=True, timeout=15)
assert verified.returncode == 0, verified.stderr
'''], cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    help_result = subprocess.run(
        [str(environment / "bin" / "floe"), "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "web" in help_result.stdout and "usage: floe" in help_result.stdout
