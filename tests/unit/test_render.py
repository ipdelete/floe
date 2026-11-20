"""Rendering the service unit `floe install` writes.

Pure text: no files, no systemd. Expected quoting follows systemd.service(5)
"Command lines" and systemd.unit(5) "Specifiers".
"""

import pytest

import floe

PYTHON = "/opt/venv/bin/python"


def service(script, **settings):
    deployment = floe.Deployment("nightly", script, ("daily",), python=PYTHON, **settings)
    return deployment.render_units()["nightly.service"]


def directive(text, key):
    return next(line.removeprefix(f"{key}=") for line in text.splitlines() if line.startswith(f"{key}="))


def test_service_runs_floe_run_on_the_script_with_the_deploying_interpreter():
    assert directive(service("/srv/flows/nightly.py"), "ExecStart") == \
        f"{PYTHON} -m floe_cli run /srv/flows/nightly.py"


def test_relative_script_is_made_absolute(tmp_path, monkeypatch):
    # systemd doesn't run the service from the directory `floe install` ran in.
    monkeypatch.chdir(tmp_path)

    assert directive(service("nightly.py"), "ExecStart").endswith(f" run {tmp_path / 'nightly.py'}")


@pytest.mark.parametrize(("script", "argument"), [
    ("/srv/my flows/nightly.py", '"/srv/my flows/nightly.py"'),
    ("/srv/it's/nightly.py", '"/srv/it\'s/nightly.py"'),
    ('/srv/say "hi"/nightly.py', '"/srv/say \\"hi\\"/nightly.py"'),
    ("/srv/100%/nightly.py", "/srv/100%%/nightly.py"),
    ("/srv/$HOME/nightly.py", "/srv/$$HOME/nightly.py"),
])
def test_script_path_is_quoted_for_systemd(script, argument):
    assert directive(service(script), "ExecStart") == f"{PYTHON} -m floe_cli run {argument}"


def test_service_is_oneshot():
    # `systemctl start` then waits for the run and fails when it fails.
    assert directive(service("/srv/flows/nightly.py"), "Type") == "oneshot"


def test_newline_in_script_path_is_rejected():
    # A newline would let the path add its own directives to the unit.
    with pytest.raises(floe.ValidationError):
        service("/srv/flows\nExecStartPre=/bin/false/nightly.py")
