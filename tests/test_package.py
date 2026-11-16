import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_package_has_no_runtime_dependencies_or_placeholder_command():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project.get("dependencies", []) == []
    assert "floe" not in project.get("scripts", {})


def test_deployment_imports_with_only_standard_library():
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); "
        "from floe.deploy import Deployment; "
        "assert 'pydantic' not in sys.modules"
    )
    result = subprocess.run(["/usr/bin/python3", "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
