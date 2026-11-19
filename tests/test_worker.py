"""Local subprocess evidence only; these workers are not in delegated cgroups."""

import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path
import textwrap

import pytest

import floe
from floe.results import RunResults
from floe.scheduler import TaskSchedule
from floe.worker import _evidence, observe_worker


def definition(tmp_path, body):
    path = tmp_path / 'flow.py'
    path.write_text("WORKFLOW = 'worker_test'\n" + textwrap.dedent(body))
    return floe.load(path)


def test_two_actual_workers_then_exclusive_join(tmp_path):
    (tmp_path / 'helper.py').write_text('factor = 7\n')
    workflow = definition(tmp_path, '''
        import os, subprocess, sys, time
        from pathlib import Path
        from floe import parallel
        def meet(task, peer):
            root = Path(__file__).parent
            (root / task).touch()
            deadline = time.monotonic() + 6
            while not (root / peer).exists():
                if time.monotonic() > deadline: raise TimeoutError('no overlap')
                time.sleep(.005)
        @parallel
        def a():
            from helper import factor
            meet('A', 'B')
            os.write(2, b'x' * 200000)
            print('tail-A', end='')
            return {'factor': factor, 'big': 'q' * 200000}
        @parallel
        def b():
            meet('B', 'A')
            subprocess.run([sys.executable, '-c', 'print("child-B")'], check=True, timeout=4)
            return None
        def c(*, a, b):
            assert a['factor'] == 7 and len(a['big']) == 200000 and b is None
            print(os.environ['FLOE_RUN_ID'])
        tasks = {'A': a, 'B': b, 'C': c}
        dag = dict.fromkeys(tasks, ())
        inputs = {'C': {'a': 'A', 'b': 'B'}}
    ''')
    schedule = TaskSchedule(workflow, 2)
    results = RunResults(workflow.inputs)
    output = {t: bytearray() for t in workflow.tasks}

    async def invoke(task):
        return await observe_worker(workflow, task, results, 'join-id',
                                    lambda t, chunk: output[t].extend(chunk), timeout=10)

    async def drive():
        first = [schedule.next_task(), schedule.next_task()]
        assert set(first) == {'A', 'B'} and schedule.next_task() is None
        for i, done in enumerate(asyncio.as_completed([invoke(t) for t in first])):
            evidence = await done
            assert evidence.returncode == 0 and evidence.error is None, evidence
            # These controlled tasks own no surviving descendants. A real managed
            # supervisor must separately check that before these two commits.
            results.complete(evidence.task, json.loads(evidence.value))
            schedule.finish(evidence.task, 'succeeded')
            if i == 0:
                assert schedule.next_task() is None
        assert schedule.next_task() == 'C'
        join = await invoke('C')
        assert (join.returncode, join.error, join.value) == (0, None, None)
        schedule.finish('C', 'succeeded')
    asyncio.run(drive())
    assert output == {'A': b'x' * 200000 + b'tail-A', 'B': b'child-B\n', 'C': b'join-id\n'}
    assert schedule.run_outcome == 'succeeded'


@pytest.mark.parametrize('action,rc,diagnostic', [
    ('raise ValueError("bad input")', 1, 'bad input'),
    ('os._exit(0)', 0, 'missing terminal'),
    ('atexit.register(os._exit, 17); return 8', 17, 'exited unsuccessfully'),
    ('return (1, 2)', 1, 'expected a JSON value'),
    ('return "x" * (1 << 20)', 1, 'per-result byte limit'),
])
def test_dont_release_result_on_missing_or_contradictory_evidence(tmp_path, action, rc, diagnostic):
    workflow = definition(tmp_path, f'''
        import atexit, os
        def a():
            print('{{"status":"succeeded","value":17}}')
            {action}
        tasks = {{'A': a, 'C': lambda x: x}}
        dag = dict.fromkeys(tasks, ())
        inputs = {{'C': {{'x': 'A'}}}}
    ''')
    s = TaskSchedule(workflow, 2)
    assert s.next_task() == 'A'
    output = bytearray()
    result = asyncio.run(observe_worker(workflow, 'A', RunResults(workflow.inputs), 'test',
                                        lambda t, b: output.extend(b), timeout=6))
    assert result.returncode == rc and result.value is None and diagnostic in result.error
    assert json.loads(output)['value'] == 17  # not a terminal message
    s.finish('A', 'failed')
    assert s.next_task() is None and s.outcomes['C'] == 'skipped'


def test_callback_failure_reaps_worker(tmp_path):
    workflow = definition(tmp_path, '''
        import os, time
        def task():
            print(os.getpid())
            time.sleep(20)
        tasks = {'A': task}
        dag = {'A': set()}
    ''')
    seen = bytearray()
    def consume(task, data):
        seen.extend(data)
        if b'\n' in seen: raise RuntimeError('sink failed')
    with pytest.raises(RuntimeError, match='sink failed'):
        asyncio.run(observe_worker(workflow, 'A', RunResults({}), 'test', consume, timeout=5))
    assert not Path(f'/proc/{int(seen)}').exists()


def test_open_output_writer_times_out_even_after_clean_worker_exit(tmp_path):
    # Hold a copy of the output writer outside the process. Same EOF semantics
    # as a detached descendant, with no orphan/zombie fixture left in PID 1.
    with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sink:
        address = str(Path(directory) / 'sink')
        sink.bind(address)
        sink.settimeout(1)
        workflow = definition(tmp_path, f'''
            import os, socket
            def a():
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                    sock.connect({address!r})
                    socket.send_fds(sock, [str(os.getpid()).encode()], [1])
            tasks = {{'A': a}}
            dag = {{'A': ()}}
        ''')
        with pytest.raises(TimeoutError):
            asyncio.run(observe_worker(workflow, 'A', RunResults({}), 'test', lambda *args: None, timeout=1.5))
        pid, fds, _, _ = socket.recv_fds(sink, 64, 1)
        try:
            assert len(fds) == 1
            with pytest.raises(ChildProcessError):
                os.waitpid(int(pid), os.WNOHANG)
        finally:
            for fd in fds:
                os.close(fd)


def test_changed_prerequisites_rejected_before_task_call(tmp_path):
    body = "tasks = {'A': lambda: print('called'), 'B': lambda: 1}\ndag = {'A': (), 'B': ()}\n"
    workflow = definition(tmp_path, body)
    workflow.source.write_text(workflow.source.read_text().replace("'A': ()", "'A': ('B',)"))
    output = []
    evidence = asyncio.run(observe_worker(workflow, 'A', RunResults({}), 'test',
                                          lambda *data: output.append(data), timeout=5))
    assert evidence.returncode == 1 and 'definition no longer matches' in evidence.error
    assert not output


def test_unsupported_retry_is_rejected_before_task_call(tmp_path):
    workflow = definition(tmp_path, '''
        from floe import RetryPolicy
        tasks = {'A': lambda: None}
        dag = {'A': ()}
        retries = {'A': RetryPolicy(2, 0, (Exception,))}
    ''')
    with pytest.raises(floe.ValidationError, match='does not support retries'):
        asyncio.run(observe_worker(workflow, 'A', RunResults({}), 'test', lambda *args: None, timeout=1))


@pytest.mark.parametrize('frame', [b'', b'X' * 16 + b'V\0\0\0\x011',
                                   b'N' * 16 + b'V\0\0\0\x021',
                                   b'N' * 16 + b'V\0\0\0\x03NaN'])
def test_invalid_result_frames(frame):
    result = _evidence('A', b'N' * 16, True, 0, frame)
    assert result.error and result.value is None
