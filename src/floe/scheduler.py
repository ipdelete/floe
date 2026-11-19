"""Private DAG admission state for a serialized supervisor. No worker or I/O here."""

from collections import deque
from graphlib import TopologicalSorter
from types import MappingProxyType
from typing import Literal

from .model import ValidationError, Workflow

Outcome = Literal['succeeded', 'failed', 'cancelled', 'skipped']


class TaskSchedule:
    """Pull one admission at a time; report completion only after worker cleanup.

    Call next_task until it returns None after every change, including stop
    requests. None can mean waiting for active workers, not necessarily done.
    An admitted task occupies a slot immediately (launch failure must finish it
    as failed). Requests identify groups to stop; they are not exit evidence.
    """

    def __init__(self, workflow: Workflow, limit: int):
        if type(limit) is not int or limit < 1:
            raise ValidationError(['worker limit must be a positive integer'])
        self._limit = limit
        self._dag = workflow.dag
        self._eligible = workflow.parallel_tasks
        self._graph = TopologicalSorter(self._dag)
        self._graph.prepare()
        self._ready = deque(self._graph.get_ready())
        self._active: dict[str, None] = {}  # admission order for cancellation
        self._exclusive_active = False
        self._stopping: set[str] = set()
        self._cancel_run = False
        self._failed = False
        self._outcomes: dict[str, Outcome] = {}

    @property
    def outcomes(self):
        return MappingProxyType(self._outcomes)

    @property
    def run_outcome(self) -> Literal['succeeded', 'failed', 'cancelled'] | None:
        if not self.done:
            return None
        if self._failed:
            return 'failed'
        if self._cancel_run or 'cancelled' in self._outcomes.values():
            return 'cancelled'
        return 'succeeded'

    @property
    def done(self) -> bool:
        return not self._graph.is_active()

    def _record(self, task: str, outcome: Outcome) -> None:
        self._outcomes[task] = outcome
        self._graph.done(task)  # processed, not necessarily succeeded
        self._ready.extend(self._graph.get_ready())

    def next_task(self) -> str | None:
        while self._ready:
            task = self._ready[0]
            if self._failed or self._cancel_run or any(self._outcomes[p] != 'succeeded' for p in self._dag[task]):
                self._ready.popleft()
                self._record(task, 'skipped')
                continue
            if len(self._active) >= self._limit:
                return None
            if self._active and (task not in self._eligible or self._exclusive_active):
                # An exclusive admission has no peers. Never bypass an exclusive
                # task at the queue head by admitting a later marked task.
                return None
            self._ready.popleft()
            self._active[task] = None
            self._exclusive_active = task not in self._eligible
            return task
        return None

    def cancellation_requested(self, task: str) -> bool:
        """Whether an active task has an accepted stop request, not proof it stopped."""
        return task in self._stopping

    def cancel_task(self, task: str) -> bool:
        """True only for a newly accepted request on an active task."""
        if task not in self._active or task in self._stopping:
            return False
        self._stopping.add(task)
        return True

    def cancel_workflow(self) -> tuple[str, ...]:
        """Latch before returning active tasks which still need a stop request."""
        if self.done:
            return ()
        self._cancel_run = True
        targets = []
        for task in self._active:
            if self.cancel_task(task):
                targets.append(task)
        return tuple(targets)

    def finish(self, task: str, outcome: Literal['succeeded', 'failed', 'cancelled']) -> None:
        if task not in self._active:
            raise ValidationError([f'no active execution for {task!r}'])
        if outcome not in ('succeeded', 'failed', 'cancelled'):
            raise ValidationError([f'invalid completion outcome {outcome!r}'])
        if outcome != 'failed' and (outcome == 'cancelled') != (task in self._stopping):
            raise ValidationError([f'completion for {task!r} contradicts its cancellation state'])
        del self._active[task]
        self._exclusive_active = False
        self._stopping.discard(task)
        if outcome == 'failed':
            self._failed = True
        self._record(task, outcome)
