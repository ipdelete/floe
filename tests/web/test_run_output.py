"""Live browser journey: dashboard -> workflow -> run -> task output."""

import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.live, pytest.mark.web]

FLOE = str(Path(sys.executable).parent / "floe")


def floe(*args, check=True):
    return subprocess.run([FLOE, *args], check=check, capture_output=True, text=True, timeout=120)


@pytest.fixture
def dashboard(tmp_path):
    deadline = time.monotonic() + 15
    log_path = tmp_path / "web-server.log"
    while time.monotonic() < deadline:
        # The CLI requires a nonzero port. Retry if it is claimed before startup.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with log_path.open("w") as log:
            process = subprocess.Popen([FLOE, "web", "--port", str(port)], stdout=log, stderr=log)
            try:
                while time.monotonic() < deadline:
                    output = log_path.read_text()
                    if process.poll() is not None:
                        if "Address already in use" in output:
                            break
                        pytest.fail(f"Dashboard exited during startup:\n{output}")
                    # The CLI prints this only after binding its own server socket.
                    if f"Floe: {url}/" in output:
                        try:
                            with urlopen(url + "/style.css", timeout=1) as response:
                                ready = response.status == 200
                        except (URLError, TimeoutError):
                            pass
                        else:
                            if ready:
                                yield url
                                return
                    time.sleep(0.05)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    pytest.fail(f"Dashboard did not become ready:\n{log_path.read_text()}")


def test_can_navigate_to_a_runs_task_output(tmp_path, page, dashboard):
    name = f"floe-web-{uuid.uuid4().hex[:12]}"
    message = f"Task output from {name}"
    workflow = tmp_path / "workflow.py"
    workflow.write_text(
        f"WORKFLOW = {name!r}\n"
        'SCHEDULE = ["2099-01-01"]\n'
        "def greet():\n"
        f"    print({message!r})\n"
        'tasks = {"greet": greet}\n'
        'dag = {"greet": set()}\n'
    )

    try:
        floe("install", str(workflow))
        floe("start", str(workflow))

        page.goto(dashboard)
        page.get_by_role("link", name=name, exact=True).click()
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
        run = page.get_by_role("row").filter(has=page.get_by_role("cell", name="Succeeded", exact=True))
        run.get_by_role("link").click()

        expect(page.get_by_role("heading", name="Run succeeded", exact=True)).to_be_visible()
        task = page.get_by_role("row").filter(has=page.get_by_role("link", name="greet", exact=True))
        expect(task.get_by_role("cell", name="Succeeded", exact=True)).to_be_visible()
        logs = page.get_by_role("list", name="Run logs")
        expect(logs.get_by_text(message, exact=True)).to_be_visible()
    finally:
        cleanup = floe("uninstall", str(workflow), check=False)
        assert cleanup.returncode == 0, f"Could not uninstall {name}: {cleanup.stderr}"
