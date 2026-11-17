import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).parents[1]


def test_no_runtime_dependencies_and_floe_command():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]
    assert project.get("dependencies", []) == []
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
            f"floe_web/icons/{path.name}" for path in expected.glob("*.svg")}
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
        ["uv", "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    rendered = subprocess.run(
        [str(python), "-I", "-c", '''
import sys
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
'''], cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    help_result = subprocess.run(
        [str(environment / "bin" / "floe"), "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "web" in help_result.stdout and "usage: floe" in help_result.stdout
