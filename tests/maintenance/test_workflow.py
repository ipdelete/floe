"""Workflow contracts use real Git histories and the real Floe runner."""

import importlib.util
import subprocess

import pytest

import floe


def execute(checkout):
    events = []
    succeeded = floe.run(floe.load(checkout.workflow), events.append)
    return succeeded, events


def test_updates_verifies_then_publishes(checkout):
    c = checkout
    succeeded, events = execute(c)
    assert succeeded
    assert c.git(c.local, "rev-parse", "HEAD") == c.target
    assert c.git(c.origin, "rev-parse", "main") == c.target
    assert c.git(c.local, "status", "--porcelain") == ""
    assert c.calls.read_text().splitlines() == ["sync", "doctor"]
    assert [e.task for e in events if e.event == "task_started"] == [
        "preflight", "fetch", "inspect", "fast-forward", "sync", "doctor", "push", "verify"]


def test_failed_doctor_retries_without_pushing_and_next_run_recovers(checkout):
    c = checkout
    c.failure.touch()
    succeeded, events = execute(c)
    assert not succeeded
    assert c.git(c.local, "rev-parse", "HEAD") == c.target
    assert c.git(c.origin, "rev-parse", "main") == c.old
    assert c.calls.read_text().splitlines() == ["sync", "doctor", "doctor"]
    assert events[-1].status == "failed"
    c.failure.unlink()
    assert execute(c)[0]
    assert c.git(c.origin, "rev-parse", "main") == c.target


@pytest.mark.parametrize("problem", ["dirty", "branch", "local-commit", "fork-ahead"])
def test_unsafe_checkout_stops_before_sync_or_push(checkout, problem):
    c = checkout
    if problem == "dirty":
        (c.local / "untracked.txt").write_text("do not remove\n")
    elif problem == "branch":
        c.git(c.local, "switch", "-c", "work")
    elif problem == "local-commit":
        c.git(c.local, "config", "user.name", "Floe test")
        c.git(c.local, "config", "user.email", "floe@example.invalid")
        (c.local / "local.txt").write_text("local change\n")
        c.git(c.local, "add", ".")
        c.git(c.local, "commit", "-m", "local only")
    else:
        c.git(c.author, "push", str(c.origin), "main")
    before = c.git(c.local, "rev-parse", "HEAD")
    fork = c.git(c.origin, "rev-parse", "main")
    assert not execute(c)[0]
    assert c.git(c.local, "rev-parse", "HEAD") == before
    assert c.git(c.origin, "rev-parse", "main") == fork
    assert not c.calls.exists()


def test_dependency_failure_does_not_retry_or_push(checkout):
    c = checkout
    uv = c.workflow.parent / "uv"
    uv.write_text("#!/bin/sh\necho 'dependency resolution failed' >&2\nexit 1\n")
    succeeded, events = execute(c)
    assert not succeeded
    assert c.git(c.origin, "rev-parse", "main") == c.old
    assert not c.calls.exists()
    assert len([e for e in events if e.event == "task_started" and e.task == "sync"]) == 1


@pytest.fixture
def module(checkout):
    spec = importlib.util.spec_from_file_location("maintenance_test", checkout.workflow)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("detail,retry", [
    ("fatal: Could not resolve host: github.com", True),
    ("fatal: unable to access: The requested URL returned error: 503", True),
    ("fatal: Authentication failed", False),
    ("! [rejected] main -> main (fetch first)", False),
])
def test_network_retry_classification(module, monkeypatch, detail, retry):
    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 128, "", detail)
    monkeypatch.setattr(module.subprocess, "run", failed)
    with pytest.raises(RuntimeError) as error:
        module.command(module.GIT, "fetch", "upstream", network=True)
    assert isinstance(error.value, module.TransientFailure) is retry


def test_network_timeout_can_retry(module, monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(module.TransientFailure, match="timed out"):
        module.command(module.GIT, "push", "origin", "main:main", network=True)


def test_checkout_change_after_doctor_prevents_push(checkout, module):
    c = checkout
    revision = {"old": c.old, "target": c.target}
    c.git(c.local, "fetch", "upstream")
    module.fast_forward(revision=revision)
    c.git(c.local, "reset", "--hard", c.old)
    with pytest.raises(RuntimeError, match="Checkout changed"):
        module.push(revision=revision)
    assert c.git(c.origin, "rev-parse", "main") == c.old
