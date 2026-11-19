"""Supervisor integration tests; fake worker containment, use real control sockets."""

import asyncio
from contextlib import asynccontextmanager
import io
import shlex

import pytest

import floe
from floe import managed
from floe.worker import WorkerEvidence
from floe_cli.main import main

RUN = '1c2a' * 8


def definition(tmp_path):
    p = tmp_path / 'flow.py'
    p.write_text('''
from floe import parallel
WORKFLOW = 'demo'
SCHEDULE = ['hourly']
@parallel
def marked(): pass

def join(*, value): pass

tasks = {'A': marked, 'B': marked, 'C': join, 'D': join}
dag = dict.fromkeys(tasks, ())
inputs = {'C': {'value': 'A'}, 'D': {'value': 'B'}}
''')
    return p


@pytest.mark.parametrize('whole', [False, True])
def test_control_reaches_supervisor_before_terminal_commit(tmp_path, monkeypatch, whole):
    @asynccontextmanager
    async def delegated(_):
        yield
    seen = []
    events = []
    w = floe.load(definition(tmp_path))
    monkeypatch.setenv('INVOCATION_ID', RUN)
    monkeypatch.setattr(managed, 'managed_group', delegated)

    async def it():
        running = asyncio.Event()
        unblock = asyncio.Event()
        async def worker(w, task, store, run, output, **settings):
            seen.append((task, store.arguments(task)))
            if task == 'A':
                output(task, b'partial\xe2')
                try:
                    await unblock.wait()
                finally:
                    output(task, b'\x98\x83')
            if task == 'B':
                running.set()
                await unblock.wait()
                return WorkerEvidence(task, 0, b'27', None)
            return WorkerEvidence(task, 0, None, None)
        monkeypatch.setattr(managed, 'observe_managed_worker', worker)
        import os
        run = asyncio.create_task(floe.run_managed(w, 2, events.append))
        try:
            await asyncio.wait_for(running.wait(), 2)
            with pytest.raises(floe.FloeError, match='not owned'):
                await asyncio.to_thread(managed.request_cancel, RUN, os.getpid() + 1, None)
            await asyncio.to_thread(managed.request_cancel, RUN, os.getpid(), None if whole else 'A')
            unblock.set()
            assert not await asyncio.wait_for(run, 3)
        finally:
            if not run.done():
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    asyncio.run(it())
    assert dict(seen) == ({'A': {}, 'B': {}} if whole else {'A': {}, 'B': {}, 'D': {'value': 27}})
    assert {e.task: e.status for e in events if e.event == 'task_completed'} == {
        'A': 'cancelled', 'B': 'cancelled' if whole else 'succeeded',
        'C': 'skipped', 'D': 'skipped' if whole else 'succeeded'}
    assert events[-1].status == 'cancelled'
    assert [e.message for e in events if e.event == 'task_output'] == ['partial☃']
    assert [floe.Event.from_dict(e.to_dict()) for e in events] == events


def test_install_opt_in_is_recorded_and_respected_in_dry_run(tmp_path, capsys):
    p = definition(tmp_path)
    store = floe.UnitStore(tmp_path / 'units')
    w = floe.load(p)
    dep = floe.Deployment.for_workflow(w, workers=2)
    plan = floe.plan_install(dep, store)
    for unit in plan.writes:
        store.write(unit.name, unit.text)
    assert floe.installed_workers(store, 'demo') == 2
    assert main(['install', 'demo', '--dry-run'], store=store) == 0
    out = capsys.readouterr().out
    assert 'Delegate=yes' in out and 'Workers=2' in out
    cmd, = [line.partition('=')[2] for line in out.splitlines() if line.startswith('ExecStart=')]
    assert shlex.split(cmd)[3:] == ['_managed-run', str(p), '2', 'demo']
    assert main(['install', 'demo', '--workers', '1', '--dry-run'], store=store) == 0
    assert 'Delegate=yes' not in capsys.readouterr().out
    assert floe.check_installed(floe.find_installed(store, 'demo'), store) == []
