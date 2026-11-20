"""Contracts between the rendered service unit, systemd, and the floe command.

C2  render -> systemd:      the unit files are ones systemd accepts.
C1  render -> floe_cli:     systemd runs the rendered ExecStart and it runs the workflow.
C5  floe_cli -> systemd:    a failed run fails the service start that floe waits on.
"""

import subprocess
import uuid

import pytest

import floe

FAILING_TASK = (
    "def boom():\n"
    '    raise RuntimeError("boom")\n'
    'tasks = {"boom": boom}\n'
    'dag = {"boom": set()}\n'
)


def render(tmp_path, body):
    """Write a workflow and render its units the way `floe install` does."""
    name = f"floe-contract-{uuid.uuid4().hex[:12]}"
    script = tmp_path / "workflow.py"
    script.write_text(f'WORKFLOW = {name!r}\nSCHEDULE = ["2099-01-01"]\n{body}')
    return floe.Deployment.for_workflow(floe.load(script)).render_units()


@pytest.fixture
def linked_service(tmp_path):
    """Link a rendered service into the user manager; unlink it afterwards."""
    linked = []

    def link(body):
        units = render(tmp_path, body)
        service = next(name for name in units if name.endswith(".service"))
        path = tmp_path / service
        path.write_text(units[service])
        subprocess.run(["systemctl", "--user", "link", str(path)], check=True, capture_output=True)
        linked.append(service)
        return service

    yield link
    for service in linked:
        subprocess.run(["systemctl", "--user", "reset-failed", service], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", service], capture_output=True)


def test_rendered_units_pass_systemd_verify(tmp_path):
    units = render(tmp_path, FAILING_TASK)
    for name, text in units.items():
        (tmp_path / name).write_text(text)

    result = subprocess.run(["systemd-analyze", "--user", "verify", *(str(tmp_path / name) for name in units)],
                            capture_output=True, text=True, timeout=30)

    # verify exits 0 on directives it ignores; problems only show on stderr.
    assert result.stderr == ""


@pytest.mark.live
def test_rendered_service_runs_the_workflow(linked_service, tmp_path):
    marker = tmp_path / "ran"
    service = linked_service(
        "def touch():\n"
        f"    open({str(marker)!r}, 'w').write('ran')\n"
        'tasks = {"touch": touch}\n'
        'dag = {"touch": set()}\n'
    )

    floe.Systemd().start(service)

    assert marker.read_text() == "ran"


@pytest.mark.live
def test_failed_run_fails_the_service_start(linked_service):
    service = linked_service(FAILING_TASK)

    with pytest.raises(floe.SystemdError):
        floe.Systemd().start(service)
