import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_no_runtime_dependencies_and_floe_command():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]
    assert project.get("dependencies", []) == []
    assert project["scripts"] == {"floe": "floe_cli.main:main"}
    assert pyproject["tool"]["uv"]["build-backend"]["module-name"] == ["floe", "floe_cli", "floe_web"]
