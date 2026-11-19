"""Installed parallel supervisor. Systemd owns the outer run deadline."""

import asyncio
import codecs
from collections.abc import Callable
from datetime import UTC, datetime
import json
import os
import re
import socket
import struct

from .cgroups import managed_group
from .model import Event, FloeError, ValidationError, Workflow
from .results import RunResults
from .scheduler import TaskSchedule
from .worker import observe_managed_worker

_RUN_ID = re.compile(r'[0-9a-f]{32}')
_CONTROL_LIMIT = 8192
_CANCEL_TIMEOUT = 3
_DRAIN = 2
_CLEANUP = 4
_CREDS = struct.Struct('3i')


def _address(run_id):
    if not _RUN_ID.fullmatch(run_id):
        raise ValidationError(['expected the full systemd invocation ID'])
    return f'\0floe-{os.geteuid()}-{run_id}'


def _peer(sock):
    return _CREDS.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CREDS.size))[:2]


def check_managed(workflow: Workflow, workers: int) -> None:
    if type(workers) is not int or workers < 2:
        raise ValidationError(['parallel execution requires an integer worker limit of at least 2'])
    if workflow.source is None or workflow.retries:
        raise ValidationError(['parallel execution requires a workflow file without retry policies'])
    if any(len(name) > 512 for name in workflow.tasks):
        raise ValidationError(['parallel task names must be at most 512 characters'])


def request_cancel(run_id: str, pid: int, task: str | None) -> None:
    payload = json.dumps(task).encode() + b'\n'
    if len(payload) > _CONTROL_LIMIT:
        raise ValidationError(['cancellation request too large'])
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(_CANCEL_TIMEOUT)
        sock.connect(_address(run_id))
        if _peer(sock) != (pid, os.geteuid()):
            raise FloeError('cancellation endpoint is not owned by the active supervisor')
        sock.sendall(payload)
        with sock.makefile('rb') as answer:
            reply = answer.readline(256)
        if reply != b'accepted\n':
            raise FloeError(reply.decode('ascii', 'replace').strip() or 'cancellation not acknowledged')


class _Output:
    """At most one partial line per worker; preserve UTF-8 across read boundaries."""

    def __init__(self, send):
        self.send = send
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''

    def feed(self, chunk, *, final=False):
        text = self.pending + self.decoder.decode(chunk, final=final)
        parts = text.split('\n')
        for part in parts[:-1]:
            while len(part) > 1024:
                self.send(part[:1024])
                part = part[1024:]
            self.send(part)
        self.pending = parts[-1]
        while len(self.pending) > 1024:
            self.send(self.pending[:1024])
            self.pending = self.pending[1024:]
        if final and self.pending:
            self.send(self.pending)
            self.pending = ''


async def run_managed(workflow: Workflow, workers: int, on_event: Callable[[Event], None]) -> bool:
    """Use only from the installed service entry point, not the serial runner."""
    check_managed(workflow, workers)
    if not hasattr(os, 'memfd_create') or not hasattr(os, 'pidfd_open') or not hasattr(socket, 'SO_PEERCRED'):
        raise FloeError('parallel execution requires Linux memfd, pidfd and Unix peer credentials')
    run_id = os.environ.get('INVOCATION_ID', '')
    address = _address(run_id)
    schedule = TaskSchedule(workflow, workers)
    results = RunResults(workflow.inputs)
    active = {}
    cancelling = set()
    clients = set()
    skipped = set()

    def emit(kind, **fields):
        on_event(Event(kind, workflow.name, run_id, datetime.now(UTC).isoformat(), schema=3, **fields))

    async def control(reader, writer):
        client = asyncio.current_task()
        clients.add(client)
        try:
            if _peer(writer.get_extra_info('socket'))[1] != os.geteuid():
                return
            async with asyncio.timeout(_CANCEL_TIMEOUT):
                data = await reader.readline()
                if not data.endswith(b'\n'):
                    return
                target = json.loads(data)
                if target is None and not schedule.done:
                    targets = schedule.cancel_workflow()
                elif isinstance(target, str) and schedule.cancel_task(target):
                    targets = (target,)
                else:
                    writer.write(b'no matching active task or workflow\n')
                    await writer.drain()
                    return
                for name in targets:
                    cancelling.add(name)
                    active[name].cancel()
                writer.write(b'accepted\n')
                await writer.drain()
        except (ValueError, OSError):
            pass
        finally:
            writer.close()
            clients.discard(client)

    async def observe(name):
        output = _Output(lambda message: emit('task_output', task=name, message=message))
        try:
            return await observe_managed_worker(workflow, name, results, run_id, lambda _, data: output.feed(data),
                                                timeout=None, drain_timeout=_DRAIN, cleanup_timeout=_CLEANUP)
        finally:
            output.feed(b'', final=True)

    # Probe the exact controls before reporting task starts, even for an empty DAG.
    async with managed_group(_CLEANUP):
        pass
    listener = await asyncio.start_unix_server(control, path=address, limit=_CONTROL_LIMIT, backlog=8)
    try:
        emit('run_started')
        error = None
        while not schedule.done:
            while (name := schedule.next_task()) is not None:
                emit('task_started', task=name)
                active[name] = asyncio.create_task(observe(name))
            for name, state in schedule.outcomes.items():
                if state == 'skipped' and name not in skipped:
                    skipped.add(name)
                    results.release_inputs(name)
                    emit('task_completed', task=name, status='skipped')
            if not active:
                break
            await asyncio.wait(active.values(), return_when=asyncio.FIRST_COMPLETED)
            for name, worker in list(active.items()):
                if not worker.done():
                    continue
                detail = None
                try:
                    evidence = worker.result()
                    if evidence.error is None:
                        if name not in cancelling:
                            results.commit_encoded(name, evidence.value)
                            status = 'succeeded'
                        else:
                            status = 'cancelled'
                    else:
                        status, detail = 'failed', evidence.error
                except asyncio.CancelledError:
                    if name not in cancelling:
                        raise
                    status = 'cancelled'
                except TimeoutError:
                    status, detail = 'failed', 'task output or descendants did not drain before deadline'
                except (FloeError, OSError) as exc:
                    status, detail = 'failed', str(exc)
                if status != 'succeeded':
                    results.release_inputs(name)
                del active[name]
                schedule.finish(name, status)
                emit('task_completed', task=name, status=status, error=detail)
                error = error or detail
        outcome = schedule.run_outcome
        emit('run_completed', status=outcome, error=error or ('task failed' if outcome == 'failed' else None))
        return outcome == 'succeeded'
    finally:
        listener.close()
        await listener.wait_closed()
        for client in clients.copy():
            client.cancel()
        for worker in active.values():
            if not worker.cancelling():
                worker.cancel()
        await asyncio.gather(*active.values(), *clients, return_exceptions=True)
