"""The examples the README points at still work. No service manager involved."""

import json
import runpy
from pathlib import Path

import floe
from floe_cli.main import main

EXAMPLES = Path(__file__).parents[1] / "examples"


def test_example_workflow_runs(capsys):
    workflow = floe.load(EXAMPLES / "workflow.py")
    assert workflow.name == "mydag" and workflow.schedule
    assert main(["run", str(EXAMPLES / "workflow.py")]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["event"] == "run_completed" and events[-1]["status"] == "succeeded"
    completed = [e["task"] for e in events if e["event"] == "task_completed"]
    assert completed == list(workflow.order)


def test_deploy_demo_plans_without_writing(capsys):
    runpy.run_path(str(EXAMPLES / "deploy_demo.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert "# mydag.service: create" in out and "# mydag.timer: create" in out
    assert not floe.user_unit_dir().exists()
