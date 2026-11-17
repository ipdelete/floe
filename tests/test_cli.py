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


def test_unhashable_prerequisites_are_a_validation_error(tmp_path, capsys):
    path = tmp_path / "flow.py"
    path.write_text('WORKFLOW = "demo"\ntasks = {"a": lambda: None}\ndag = {"a": [["b"]]}\n')
    assert main(["run", str(path)]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "error: invalid workflow:" in err and "nonempty strings" in err
    assert "Traceback" not in err


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


def test_uninstall_without_files_reports_manager_query_failure(store, sd, capsys):
    sd.show.side_effect = SystemdError(["systemctl", "show"], 1, "cannot connect to bus")
    assert main(["uninstall", "mydag"], store=store, sd=sd) == 1
    out, err = capsys.readouterr()
    assert out == "removed: nothing\n"
    assert "cannot connect to bus" in err and "rerun uninstall" in err


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
    journal = history_journal()
    assert main(["runs", "mydag", "--since", "today", "-n", "5"], sd=sd, journal=journal) == 0
    assert journal.calls == [
        ("mydag.service", "today", True, None),
        ("mydag.service", None, False, ("b" * 32, "a" * 32)),
    ]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["RUN", "STARTED", "STATUS", "DURATION", "DETAIL"]
    assert lines[1].startswith("bbbbbbbb ") and "failed" in lines[1] and lines[1].endswith("fetch: broken input")
    assert lines[2].startswith("aaaaaaaa ") and "succeeded" in lines[2]
    assert len(lines) == 3

    assert main(["runs", "mydag", "-n", "1"], sd=sd, journal=journal) == 0
    limited = capsys.readouterr().out.splitlines()
    assert [line.split() for line in limited] == [line.split() for line in lines[:2]]
    assert journal.calls[-2:] == [
        ("mydag.service", None, True, None),
        ("mydag.service", None, False, ("b" * 32,)),
    ]


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


def list_sd(sd):
    sd.show_all.return_value = [
        ("UnitFileState", "enabled"), ("ActiveState", "active"),
        ("TimersCalendar", "{ OnCalendar=*-*-* *:00:00 ; next_elapse=@1794855600 }"),
        ("NextElapseUSecRealtime", "@1794855600"), ("LastTriggerUSec", ""),
    ]
    sd.show.return_value = {"ActiveState": "inactive"}
    return sd


def test_list(flow, store, sd, capsys):
    assert main(["list"], store=store, sd=list_sd(sd), journal=FakeJournal([])) == 0
    assert capsys.readouterr().out == "no workflows installed\n"
    main(["install", str(flow)], store=store, sd=sd)
    capsys.readouterr()
    journal = FakeJournal(Run("a" * 32).succeeded_run().records)
    assert main(["list"], store=store, sd=sd, journal=journal) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NAME", "SCHEDULE", "TIMER", "NEXT", "LAST", "RUN", "SCRIPT"]
    assert lines[1].startswith("mydag  *-*-* *:00:00  active  ")
    assert lines[1].endswith(f"succeeded  {flow}") and len(lines) == 2


def test_list_problems_and_check_exit_code(flow, store, sd, capsys):
    main(["install", str(flow)], store=store, sd=list_sd(sd))
    flow.write_text(flow.read_text().replace('"hourly"', '"daily"'))
    capsys.readouterr()
    assert main(["list"], store=store, sd=sd, journal=FakeJournal([])) == 0
    assert "  ! " not in capsys.readouterr().out
    assert main(["list", "--check"], store=store, sd=sd, journal=FakeJournal([])) == 1
    assert capsys.readouterr().out.splitlines()[2] == "  ! units out of date (mydag.timer); reinstall"
    assert main(["list", "--json"], store=store, sd=sd, journal=FakeJournal([])) == 0
    [data] = json.loads(capsys.readouterr().out)
    assert data["name"] == "mydag" and data["script"] == str(flow) and data["enabled"] is True


def test_list_aligns_multiple_workflows_and_keeps_problems_with_their_row(flow, store, sd, capsys):
    other = flow.with_name("other.py")
    other.write_text(flow.read_text().replace('WORKFLOW = "mydag"', 'WORKFLOW = "z-longer-name"'))
    for script in (flow, other):
        assert main(["install", str(script)], store=store, sd=list_sd(sd)) == 0
    flow.write_text(flow.read_text().replace('"hourly"', '"daily"'))
    other.unlink()
    capsys.readouterr()

    assert main(["list", "--check"], store=store, sd=sd, journal=FakeJournal([])) == 1
    header, first, first_problem, second, second_problem = capsys.readouterr().out.splitlines()
    assert first.startswith("mydag ") and second.startswith("z-longer-name ")
    assert first.index("*-*-*") == second.index("*-*-*") == header.index("SCHEDULE")
    assert first_problem == "  ! units out of date (mydag.timer); reinstall"
    assert second_problem == f"  ! script not found: {other}"


@pytest.mark.parametrize("command", ["run", "install", "start", "status", "runs", "logs", "uninstall"])
def test_explicit_name_target_supports_py_suffix(flow, store, sd, capsys, command):
    flow.write_text(flow.read_text().replace('WORKFLOW = "mydag"', 'WORKFLOW = "review.py"'))
    assert main(["install", str(flow)], store=store, sd=sd) == 0
    if command not in ("run", "install"):
        flow.unlink()
    capsys.readouterr()
    result = main([command, "--name", "review.py"], store=store, sd=sd, journal=FakeJournal([]))
    out, err = capsys.readouterr()
    assert result == (1 if command == "logs" else 0)
    assert "workflow file not found" not in err
    if command == "run":
        assert json.loads(out.splitlines()[-1])["workflow"] == "review.py"
    elif command == "install":
        assert "nothing (up to date)" in out
    elif command == "start":
        sd.start.assert_called_once_with("review.py.service", wait=True)
    elif command == "status":
        assert "review.py.service" in out and "review.py.timer" in out
    elif command == "runs":
        assert out == "no runs recorded\n"
    elif command == "logs":
        assert "no runs of review.py.service recorded" in err
    else:
        assert store.units() == []


@pytest.mark.parametrize("invalid", [False, True])
def test_list_check_json_routes_import_output_to_stderr(flow, store, sd, capsys, invalid):
    assert main(["install", str(flow)], store=store, sd=list_sd(sd)) == 0
    (flow.parent / "helper.py").write_text('print("helper initialized")\n')
    body = "import helper\n" + flow.read_text()
    if invalid:
        body += '\nraise RuntimeError("broken definition")\n'
    flow.write_text(body)
    capsys.readouterr()
    assert main(["list", "--check", "--json"], store=store, sd=sd, journal=FakeJournal([])) == int(invalid)
    out, err = capsys.readouterr()
    [entry] = json.loads(out)
    assert entry["name"] == "mydag"
    assert bool(entry["problems"]) is invalid
    assert "helper initialized" in err


@pytest.mark.parametrize("command", ["run", "runs", "logs"])
def test_definition_import_output_does_not_corrupt_json(flow, sd, capsys, command):
    (flow.parent / "helper.py").write_text('print("helper initialized")\n')
    flow.write_text("import helper\n" + flow.read_text())
    args = [command, str(flow)] + ([] if command == "run" else ["--json"])
    assert main(args, sd=sd, journal=history_journal()) == 0
    out, err = capsys.readouterr()
    if command == "run":
        assert json.loads(out.splitlines()[-1])["status"] == "succeeded"
        assert all(isinstance(json.loads(line), dict) for line in out.splitlines())
    else:
        assert json.loads(out)
    assert "helper initialized" in err


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_calendar_is_reported_without_writing_units(flow, store, sd, capsys, dry_run):
    flow.write_text(flow.read_text().replace('"hourly"', '"not-a-calendar"'))
    assert main(["install", str(flow), *(["--dry-run"] if dry_run else [])], store=store, sd=sd) == 1
    out, err = capsys.readouterr()
    assert out == "" and "invalid schedule entry 'not-a-calendar'" in err
    assert not store.path.exists()
    assert sd.mock_calls == []


def test_run_and_reinstall_by_name(flow, store, sd, capsys):
    main(["install", str(flow)], store=store, sd=sd)
    flow.write_text(flow.read_text().replace('"hourly"', '"daily"'))
    capsys.readouterr()
    assert main(["run", "mydag"], store=store, sd=sd) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "succeeded"
    assert main(["install", "mydag"], store=store, sd=sd) == 0
    assert capsys.readouterr().out.startswith("wrote: mydag.timer;")


@pytest.mark.parametrize("args,message", [
    (["run", "mydag"], "error: workflow 'mydag' is not installed"),
    (["install", "mydag"], "error: workflow 'mydag' is not installed"),
    (["run", "./missing"], "error: workflow file not found"),
])
def test_name_targets_must_be_installed(store, sd, capsys, args, message):
    assert main(args, store=store, sd=sd) == 1
    assert capsys.readouterr().err.startswith(message)


def test_default_unit_dir_resolved_at_call_time(flow, tmp_path, sd, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom"))
    assert main(["install", "--dry-run", str(flow)], sd=sd) == 0
    assert str(tmp_path / "custom" / "systemd" / "user" / "mydag.service") in capsys.readouterr().out


def test_console_script_installed(flow, tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    floe = Path(sys.executable).parent / "floe"
    r = subprocess.run([str(floe), "install", "--dry-run", str(flow)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "systemd").exists()


def test_service_command_runs_with_system_python_and_no_venv(flow, tmp_path):
    """Exactly what the generated unit runs: system python, PYTHONPATH=src, no site-packages."""
    r = subprocess.run(
        ["/usr/bin/python3", "-s", "-m", "floe_cli", "run", str(flow)],
        capture_output=True, text=True, cwd=tmp_path,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.splitlines()[-1])["status"] == "succeeded"


NOISY = '''import os, subprocess, sys
WORKFLOW = "noisy"
def first():
    print("printed")
    subprocess.run(["sh", "-c", "echo child"], check=True)
    os.write(1, b"raw fd\\n")
    sys.stdout.write("unterminated")
def second():
    print("second task")
    if os.environ.get("NOISY_FAIL"):
        raise RuntimeError("broken")
tasks = {"first": first, "second": second}
dag = {"first": set(), "second": {"first"}}
'''


def _run_noisy(tmp_path, *, merged: bool, fail: bool = False) -> subprocess.CompletedProcess:
    script = tmp_path / "noisy.py"
    script.write_text(NOISY)
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin", **({"NOISY_FAIL": "1"} if fail else {})}
    return subprocess.run(
        [sys.executable, "-m", "floe_cli", "run", str(script)], cwd=tmp_path, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merged else subprocess.PIPE,
    )


@pytest.mark.parametrize("fail", [False, True])
def test_task_stdout_cannot_corrupt_the_event_stream(tmp_path, fail):
    """print(), sys.stdout and child processes writing to fd 1 all end up on stderr."""
    r = _run_noisy(tmp_path, merged=False, fail=fail)
    assert r.returncode == (1 if fail else 0), r.stderr
    events = [json.loads(line) for line in r.stdout.splitlines()]
    assert [e["event"] for e in events] == [
        "run_started", "task_started", "task_completed", "task_started", "task_completed", "run_completed"]
    assert events[-1]["status"] == ("failed" if fail else "succeeded")
    for text in ("printed", "child", "raw fd", "unterminated", "second task"):
        assert text in r.stderr


def test_task_output_precedes_its_task_completed_event(tmp_path):
    """Like journald, read both streams from one pipe: output must not be held in a
    buffer and surface after its task has completed, where it is misattributed."""
    r = _run_noisy(tmp_path, merged=True)
    assert r.returncode == 0, r.stdout
    task, owners = None, {}
    for line in r.stdout.splitlines():
        # A partial line is completed by whatever is written next; split it off.
        text, brace, rest = line.partition("{")
        if text:
            owners[text] = task
        if brace:
            event = json.loads(brace + rest)
            task = event["task"] if event["event"] == "task_started" else None
    assert owners == {"printed": "first", "child": "first", "raw fd": "first",
                      "unterminated": "first", "second task": "second"}


def test_in_process_task_print_goes_to_stderr(tmp_path, capsys):
    """When sys.stdout is not file descriptor 1 (main() called in-process), print()
    in a task must still not reach the stream events are written to."""
    script = tmp_path / "flow.py"
    script.write_text("WORKFLOW = 'p'\ndef a():\n    print('from task')\ntasks = {'a': a}\ndag = {'a': set()}\n")
    assert main(["run", str(script)]) == 0
    out, err = capsys.readouterr()
    assert [json.loads(line)["event"] for line in out.splitlines()] == [
        "run_started", "task_started", "task_completed", "run_completed"]
    assert err == "from task\n"


@pytest.mark.parametrize("body", ["pass", "raise RuntimeError('x')", "raise KeyboardInterrupt"])
def test_run_restores_stdout_afterwards(tmp_path, capsys, body):
    script = tmp_path / "flow.py"
    script.write_text(f"WORKFLOW = 'restore'\ndef a():\n    {body}\ntasks = {{'a': a}}\ndag = {{'a': set()}}\n")
    before_stream, before_fd = sys.stdout, os.fstat(1)
    try:
        main(["run", str(script)])
    except KeyboardInterrupt:
        pass
    assert sys.stdout is before_stream
    after_fd = os.fstat(1)
    assert (after_fd.st_dev, after_fd.st_ino) == (before_fd.st_dev, before_fd.st_ino)
    assert '"run_started"' in capsys.readouterr().out
