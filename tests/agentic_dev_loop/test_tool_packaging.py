import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]


def test_floe_wheel_excludes_loop_tool_and_tests(tmp_path):
    built = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path), str(ROOT)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    [wheel] = tmp_path.glob("*.whl")
    with ZipFile(wheel) as archive:
        leaked = [name for name in archive.namelist() if "agentic_dev_loop" in PurePosixPath(name).parts]
        assert not leaked
