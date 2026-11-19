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
    if len(size) != 4 or int.from_bytes(size) > limit:
        raise _problem('invalid frame length')
    count = int.from_bytes(size)
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


async def _wait_exit(proc):
    loop = asyncio.get_running_loop()
    fd = os.pidfd_open(proc.pid)
    ready = asyncio.Event()
    try:
        loop.add_reader(fd, ready.set)
        try:
            await ready.wait()
        finally:
            loop.remove_reader(fd)
        return proc.wait()
    finally:
        os.close(fd)


async def observe_worker(workflow: Workflow, task: str, results: RunResults, run_id: str,
                         on_output: Callable[[str, bytes], None], *, timeout: float) -> WorkerEvidence:
    """Collect one invocation. No public activation or automatic dependency release.

    Call only for controlled local fixtures until a managed launcher provides
    containment before definition loading. On exceptional exit we reap the
    direct worker, NOT arbitrary descendants. Never infer ownership from a PID.
    """
    if type(timeout) not in (int, float) or not 0 < timeout < math.inf:
        raise _problem('observation timeout must be finite and positive')
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
        proc = subprocess.Popen([sys.executable, '-u', '-c', bootstrap], stdin=subprocess.DEVNULL,
                                stdout=child_output, stderr=child_output,
                                pass_fds=(request.fileno(), child_reply.fileno()),
                                env={**os.environ, 'FLOE_RUN_ID': run_id})
        try:
            child_output.close()
            child_reply.close()
            message = bytearray()

            def append(chunk):
                if len(message) + len(chunk) > MAX_RESULT_BYTES + 21:
                    raise _problem('oversized terminal frame')
                message.extend(chunk)

            readers = [asyncio.create_task(coro) for coro in (
                _wait_exit(proc), _drain(reply, append), _drain(output, lambda data: on_output(task, data)))]
            try:
                async with asyncio.timeout(timeout):
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
