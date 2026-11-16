"""The floe command: argument handling, output formatting, and exit codes.
Library behavior is tested in test_deploy/test_runner; this only checks translation."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import call, create_autospec

import pytest
from journal_fakes import FakeJournal, Run

from floe.store import UnitStore
from floe.systemd import Systemd, SystemdError
from floe_cli.main import main

ROOT = Path(__file__).parents[1]
DEFINITION = '''import sys
WORKFLOW = "mydag"
SCHEDULE = ["hourly"]
def ok():
    print("working", file=sys.stderr)
def boom():
    raise RuntimeError("broken input")
tasks = {"ok": ok, "boom": boom}
dag = {"ok": set(), "boom": {"ok"}}
'''


@pytest.fixture
def sd():
    sd = create_autospec(Systemd, instance=True)
    sd.show.return_value = {"LoadState": "not-found", "ActiveState": "inactive", "FragmentPath": ""}
    return sd


@pytest.fixture
def store(tmp_path):
    return UnitStore(tmp_path / "units")


@pytest.fixture
def flow(tmp_path):
    path = tmp_path / "my flows" / "flow.py"
    path.parent.mkdir()
    path.write_text(DEFINITION.replace(', "boom": boom}', "}").replace(', "boom": {"ok"}}', "}"))
    return path


def test_run_prints_json_events_and_task_output(flow, capsys):
    assert main(["run", str(flow)]) == 0
    out, err = capsys.readouterr()
    events = [json.loads(line) for line in out.splitlines()]
    assert [e["event"] for e in events] == ["run_started", "task_started", "task_completed", "run_completed"]
    assert events[-1]["status"] == "succeeded"
    assert err == "working\n"


def test_run_failure_exits_nonzero(tmp_path, capsys):
    path = tmp_path / "flow.py"
    path.write_text(DEFINITION)
    assert main(["run", str(path)]) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1] == {**events[-1], "status": "failed", "error": "broken input"}


def test_invalid_definition_lists_every_problem(tmp_path, capsys):
    path = tmp_path / "flow.py"
    path.write_text('WORKFLOW = "../bad"\ntasks = {"a": None}\ndag = {"a": set()}\n')
    assert main(["run", str(path)]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "error: invalid workflow:\n  - invalid workflow name" in err
    assert "\n  - task 'a' is not callable" in err and "Traceback" not in err


def test_install_dry_run_prints_exact_files(flow, store, sd, capsys):
    assert main(["install", "--dry-run", str(flow)], store=store, sd=sd) == 0
    out = capsys.readouterr().out
    for name in ("mydag.service", "mydag.timer"):
        assert f"# {store.path / name} (create)\n" in out
    assert f"ExecStart=/usr/bin/python3 -m floe_cli run \"{flow}\"" in out
    assert not store.path.exists()
    assert sd.mock_calls == []


def test_install_dry_run_reports_conflicts(flow, store, sd, capsys):
    store.path.mkdir()
    (store.path / "mydag.timer").write_text("foreign")
    assert main(["install", "--dry-run", str(flow)], store=store, sd=sd) == 1
    assert "(conflict: not managed by floe (adopt to take over))" in capsys.readouterr().out


def test_install_and_reinstall(flow, store, sd, capsys):
    assert main(["install", str(flow)], store=store, sd=sd) == 0
    assert capsys.readouterr().out == "wrote: mydag.service, mydag.timer; enabled mydag.timer\n"
    assert main(["install", str(flow)], store=store, sd=sd) == 0
    assert "nothing (up to date)" in capsys.readouterr().out


def test_install_adopt(flow, store, sd, capsys):
    store.path.mkdir()
    (store.path / "mydag.service").write_text("foreign")
    assert main(["install", str(flow)], store=store, sd=sd) == 1
    assert "adopt to take over" in capsys.readouterr().err
    assert main(["install", "--adopt", str(flow)], store=store, sd=sd) == 0
    assert store.is_owned("mydag.service")


@pytest.mark.parametrize("by", ["path", "name"])
def test_uninstall_by_path_or_name(flow, store, sd, capsys, by):
    main(["install", str(flow)], store=store, sd=sd)
    capsys.readouterr()
    assert main(["uninstall", str(flow) if by == "path" else "mydag"], store=store, sd=sd) == 0
    assert capsys.readouterr().out == "removed: mydag.timer, mydag.service\n"


def test_uninstall_reports_partial_failure(flow, store, sd, capsys):
    main(["install", str(flow)], store=store, sd=sd)
    capsys.readouterr()
    sd.stop.side_effect = SystemdError(["systemctl", "stop"], 1, "stop timed out")
    assert main(["uninstall", "mydag"], store=store, sd=sd) == 1
    out, err = capsys.readouterr()
    assert "removed: mydag.timer, mydag.service" in out
    assert "stop timed out" in err and "rerun uninstall" in err and "Traceback" not in err


def test_start_and_status(store, sd, capsys):
    assert main(["start", "mydag"], store=store, sd=sd) == 0
    assert sd.mock_calls == [call.start("mydag.service", wait=True)]
    assert "finished successfully" in capsys.readouterr().out
    sd.show.return_value = {"ActiveState": "inactive"}
    assert main(["status", "mydag"], store=store, sd=sd) == 0
    out = capsys.readouterr().out
    assert out.startswith("mydag.service\n  ActiveState=inactive\nmydag.timer\n")


@pytest.mark.parametrize("args", [["status", "../bad"], ["run", "absent.py"], ["status", "absent.py"]])
def test_bad_targets_are_concise_errors(args, sd, capsys):
    assert main(args, sd=sd) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err
    assert sd.mock_calls == []


def test_failed_start_points_to_logs(sd, capsys):
    sd.start.side_effect = SystemdError(["systemctl", "start"], 1, "Job failed")
    assert main(["start", "mydag"], sd=sd) == 1
    assert capsys.readouterr().err == "error: mydag.service failed; see: floe logs mydag\n"


def test_runs_detail_for_failure_before_start_is_one_line(sd, capsys):
    r = Run("c" * 32).starting().output("error: invalid workflow:").output("  - unknown prerequisites: 'x'").failed()
    assert main(["runs", "mydag"], sd=sd, journal=FakeJournal(r.records)) == 0
    assert capsys.readouterr().out.splitlines()[1].endswith("invalid workflow: unknown prerequisites: 'x'")


@pytest.mark.parametrize("error", [OSError("disk error"), SystemdError(["systemctl"], 1, "no session")])
def test_expected_failures_are_reported(sd, capsys, error):
    sd.start.side_effect = error
    assert main(["start", "mydag"], sd=sd) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "Traceback" not in err


def history_journal():
    ok = Run("a" * 32).succeeded_run(tasks=("fetch",))
    bad = Run("b" * 32, ok.us + 60_000_000).starting().event("run_started").event("task_started", task="fetch")
    bad.output("fetching")
    bad.event("task_completed", task="fetch", status="failed", error="broken input")
    bad.event("run_completed", status="failed", error="broken input").failed()
    return FakeJournal(ok.records + bad.records)


def test_runs_table(sd, capsys):
    sd.show.return_value = {"ActiveState": "inactive"}
    assert main(["runs", "mydag", "--since", "today", "-n", "5"], sd=sd, journal=history_journal()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["RUN", "STARTED", "STATUS", "DURATION", "DETAIL"]
    assert lines[1].startswith("bbbbbbbb ") and "failed" in lines[1] and lines[1].endswith("fetch: broken input")
    assert lines[2].startswith("aaaaaaaa ") and "succeeded" in lines[2]


def test_runs_json_and_empty(sd, capsys):
    assert main(["runs", "mydag", "--json"], sd=sd, journal=history_journal()) == 0
    runs = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in runs] == ["failed", "succeeded"]
    assert main(["runs", "mydag"], sd=sd, journal=FakeJournal([])) == 0
    assert capsys.readouterr().out == "no runs recorded\n"


def test_logs_latest_and_by_prefix(sd, capsys):
    assert main(["logs", "mydag"], sd=sd, journal=history_journal()) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"run {'b' * 32}  failed")
    assert "error: broken input" in out
    for text in ["systemd: Starting mydag.service", "▶ run started", "▶ fetch started",
                 "[fetch] fetching", "■ fetch failed: broken input", "■ run failed: broken input"]:
        assert text in out
    assert main(["logs", "mydag", "aaaa", "--json"], sd=sd, journal=history_journal()) == 0
    assert json.loads(capsys.readouterr().out)["run"]["status"] == "succeeded"


@pytest.mark.parametrize("args,message", [
    (["logs", "mydag", "ffff"], "error: no run of mydag.service matches 'ffff'"),
    (["logs", "mydag", "XYZ"], "error: invalid run ID 'XYZ'"),
])
def test_logs_errors(sd, capsys, args, message):
    assert main(args, sd=sd, journal=history_journal()) == 1
    assert capsys.readouterr().err.startswith(message)


def test_runs_rejects_bad_limit(capsys):
    with pytest.raises(SystemExit):
        main(["runs", "mydag", "-n", "0"])
    assert "must be at least 1" in capsys.readouterr().err


def test_default_unit_dir_resolved_at_call_time(flow, tmp_path, sd, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom"))
    assert main(["install", "--dry-run", str(flow)], sd=sd) == 0
    assert str(tmp_path / "custom" / "systemd" / "user" / "mydag.service") in capsys.readouterr().out


def test_console_script_installed(tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    floe = Path(sys.executable).parent / "floe"
    r = subprocess.run([str(floe), "install", "--dry-run", str(ROOT / "workflow.py")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "systemd").exists()


def test_service_command_runs_with_system_python_and_no_venv(tmp_path):
    """Exactly what the generated unit runs: system python, PYTHONPATH=src, no site-packages."""
    r = subprocess.run(
        ["/usr/bin/python3", "-s", "-m", "floe_cli", "run", str(ROOT / "workflow.py")],
        capture_output=True, text=True, cwd=tmp_path,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.splitlines()[-1])["status"] == "succeeded"
