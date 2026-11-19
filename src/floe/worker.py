"""Private Linux worker transport; not a runner or a containment boundary.

One fresh interpreter, an anonymous in-memory request file, a one-reply socket,
and a separate combined stdout/stderr stream. The caller owns task admission,
containment, run deadlines, result publication and lifecycle events.
"""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
import json
import math
import os
import socket
import subprocess
import sys
import uuid

from .cgroups import managed_group
from .loader import load
from .model import ValidationError, Workflow
from .results import MAX_RESULT_BYTES, RunResults, _encode
from .runner import _check_result

_META_LIMIT = 65536
_ERROR_LIMIT = 4096


def _problem(message):
    return ValidationError([f'worker transport: {message}'])


def _frame(payload):
    return len(payload).to_bytes(4, 'big') + payload


def _read_frame(stream, limit):
    size = stream.read(4)
    count = int.from_bytes(size)
    if len(size) != 4 or count > limit:
        raise _problem('invalid frame length')
    data = stream.read(count)
    if len(data) != count:
        raise _problem('incomplete frame')
    return data


def _invoke(request_fd: int, reply_fd: int) -> int:
    """Child bootstrap, before executing trusted workflow code."""
    os.set_inheritable(reply_fd, False)
    with socket.socket(fileno=reply_fd) as reply:
        with os.fdopen(request_fd, 'rb') as request:
            info = json.loads(_read_frame(request, _META_LIMIT))
            bindings = info['bindings']
            encoded = {p: _read_frame(request, MAX_RESULT_BYTES) for p in sorted(set(bindings.values()))}
            if request.read(1):
                raise _problem('trailing request bytes')
        nonce = bytes.fromhex(info['execution'])
        task = info['task']
        try:
            workflow = load(info['source'])
            if (workflow.name != info['workflow'] or task not in workflow.tasks or workflow.retries
                    or workflow.inputs.get(task, {}) != bindings
                    or sorted(workflow.dag[task]) != info['prerequisites']
                    or (task in workflow.parallel_tasks) != info['parallel']):
                raise _problem('definition no longer matches request or uses unsupported retries')
            kwargs = {key: json.loads(encoded[p]) for key, p in bindings.items()}
            result = workflow.tasks[task](**kwargs)
            _check_result(result, task)
            kind, payload = (b'V', _encode(result, task)) if info['capture'] else (b'S', b'')
        except Exception as exc:
            message = str(exc)[:_ERROR_LIMIT].encode('utf-8', 'replace')[:_ERROR_LIMIT]
            reply.sendall(nonce + b'E' + _frame(message))
            return 1
        reply.sendall(nonce + kind + _frame(payload))
        return 0


@dataclass(frozen=True)
class WorkerEvidence:
    """Output EOF has been observed. Group emptiness has NOT been checked."""
    task: str
    returncode: int
    value: bytes | None  # b'null' is a real result; None is not a bound result
    error: str | None


def _evidence(task, nonce, capture, rc, message):
    error = 'invalid or missing terminal frame'
    if len(message) >= 21 and message[:16] == nonce and int.from_bytes(message[17:21]) == len(message) - 21:
        kind, payload = message[16:17], message[21:]
        if kind == b'E' and rc != 0 and len(payload) <= _ERROR_LIMIT:
            error = payload.decode('utf-8', 'replace') or 'worker reported failure'
        elif rc != 0:
            error = 'worker exited unsuccessfully'
        elif not capture and kind == b'S' and not payload:
            return WorkerEvidence(task, rc, None, None)
        elif capture and kind == b'V' and len(payload) <= MAX_RESULT_BYTES:
            try:
                if _encode(json.loads(payload), task) == payload:
                    return WorkerEvidence(task, rc, payload, None)
            except (ValueError, RecursionError):
                pass
            error = 'invalid result payload'
    return WorkerEvidence(task, rc, None, error)


async def _drain(sock, consume):
    loop = asyncio.get_running_loop()
    while chunk := await loop.sock_recv(sock, 65536):
        consume(chunk)
        # A continuously readable stream must not starve peer workers or control.
        await asyncio.sleep(0)


async def _wait_exit(proc, fd):
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    loop.add_reader(fd, ready.set)
    try:
        await ready.wait()
        return proc.wait()  # pidfd is readable: this owned child has exited
    finally:
        loop.remove_reader(fd)


async def observe_worker(workflow: Workflow, task: str, results: RunResults, run_id: str,
                         on_output: Callable[[str, bytes], None], *, timeout: float) -> WorkerEvidence:
    """Collect one invocation. No public activation or automatic dependency release.

    Call only for controlled local fixtures until a managed launcher provides
    containment before definition loading. On exceptional exit we reap the
    direct worker, NOT arbitrary descendants. An inherited writer can therefore
    cause TimeoutError after worker exit; do not treat that as a result. The
    nonce correlates evidence, not authentication against trusted user code.
    """
    _check_timeout(timeout)
    return await _observe(workflow, task, results, run_id, on_output, timeout, None, None)


def _check_timeout(value):
    if type(value) not in (int, float) or not 0 < value < math.inf:
        raise _problem('timeouts must be finite and positive')


async def observe_managed_worker(workflow: Workflow, task: str, results: RunResults, run_id: str,
                                 on_output: Callable[[str, bytes], None], *, timeout: float | None,
                                 drain_timeout: float, cleanup_timeout: float) -> WorkerEvidence:
    """Return terminal evidence only after the task group is verified empty and removed.

    On timeout, transport failure or cancellation, discard any staged reply and
    clean the owned group. A cleanup error is not a successful cancellation.
    No public launch path; requires an already delegated service cgroup.
    """
    if timeout is not None:
        _check_timeout(timeout)
    for value in (drain_timeout, cleanup_timeout):
        _check_timeout(value)
    async with managed_group(cleanup_timeout) as group:
        return await _observe(workflow, task, results, run_id, on_output, timeout, group, drain_timeout)


async def _observe(workflow, task, results, run_id, on_output, timeout, group, drain_timeout):
    if workflow.source is None or task not in workflow.tasks or workflow.retries:
        raise _problem('requires a file-backed task and does not support retries')
    nonce = uuid.uuid4().bytes
    bindings = dict(workflow.inputs.get(task, {}))
    capture = bool(results.remaining[task])
    info = json.dumps(dict(source=str(workflow.source), workflow=workflow.name, task=task,
                           prerequisites=sorted(workflow.dag[task]), parallel=task in workflow.parallel_tasks,
                           execution=nonce.hex(), bindings=bindings, capture=capture)).encode()
    if len(info) > _META_LIMIT:
        raise _problem('request metadata too large')
    with ExitStack() as stack:
        request = stack.enter_context(os.fdopen(os.memfd_create('floe-worker'), 'w+b'))
        request.write(_frame(info))
        for p in sorted(set(bindings.values())):
            if p not in results.values:
                raise _problem(f'no committed result for prerequisite {p!r}')
            request.write(_frame(results.values[p]))
        request.seek(0)
        reply, child_reply = socket.socketpair()
        stack.enter_context(reply)
        stack.enter_context(child_reply)
        output, child_output = socket.socketpair()
        stack.enter_context(output)
        stack.enter_context(child_output)
        reply.setblocking(False)
        output.setblocking(False)
        bootstrap = (f'from floe.worker import _invoke; raise SystemExit('
                     f'_invoke({request.fileno()}, {child_reply.fileno()}))')
        args = [sys.executable, '-u', '-c', bootstrap]
        fds = (request.fileno(), child_reply.fileno())
        if group is not None:
            join = group.open_control('cgroup.procs', os.O_WRONLY)
            stack.callback(os.close, join)
            fds += (join,)
            # Start isolated without site hooks, then exec the installing Python
            # normally *inside* the task group. No definition or PYTHONPATH code
            # in the pre-placement interpreter. Writing 0 migrates the writer.
            place = (f'import os, sys\nif os.write({join}, b"0") != 1: raise OSError("short cgroup write")\n'
                     f'os.close({join})\nos.execv(sys.executable, sys.argv[1:])')
            args = [sys.executable, '-I', '-S', '-c', place, *args]
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                stdout=child_output, stderr=child_output,
                                pass_fds=fds,
                                env={**os.environ, 'FLOE_RUN_ID': run_id})
        try:
            pidfd = os.pidfd_open(proc.pid)
            stack.callback(os.close, pidfd)
            child_output.close()
            child_reply.close()
            message = bytearray()

            def append(chunk):
                if len(message) + len(chunk) > MAX_RESULT_BYTES + 21:
                    raise _problem('oversized terminal frame')
                message.extend(chunk)

            async with asyncio.timeout(timeout) as budget:
                async def exit_gate():
                    rc = await _wait_exit(proc, pidfd)
                    if group is not None:
                        # Shorten, never extend, the run budget. This also bounds
                        # inherited output writers even if the group is now empty.
                        end = asyncio.get_running_loop().time() + drain_timeout
                        deadline = budget.when()
                        budget.reschedule(end if deadline is None else min(deadline, end))
                        await group.wait_empty()
                    return rc

                readers = [asyncio.create_task(coro) for coro in (
                    exit_gate(), _drain(reply, append), _drain(output, lambda data: on_output(task, data)))]
                try:
                    rc, _, _ = await asyncio.gather(*readers)
                finally:
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
            return _evidence(task, nonce, capture, rc, bytes(message))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
