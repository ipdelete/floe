"""Drive admissions and observed worker exits separately, without subprocesses."""

import inspect

import pytest

import floe
from floe.scheduler import TaskSchedule


def schedule(dag, *, marked=None, limit=2):
    @floe.parallel
    def eligible():
        pass

    def exclusive():
        pass

    tasks = {t: eligible if marked is None or t in marked else exclusive for t in dag}
    return TaskSchedule(floe.Workflow('test', tasks, dag), limit)


def admit(s):
    started = []
    while (task := s.next_task()) is not None:
        started.append(task)
    return started


def test_marker_survives_file_loading_and_preserves_sync_contract(tmp_path):
    f = tmp_path / 'flow.py'
    f.write_text('''from floe import parallel
WORKFLOW = 'demo'
@parallel
def marked(*, value=1):
    return value
tasks = {'A': marked, 'B': lambda: None}
dag = {'A': set(), 'B': {'A'}}
''')
    w = floe.load(f)
    assert w.parallel_tasks == {'A'} and w.to_dict()['parallel_tasks'] == ['A']
    assert list(inspect.signature(w.tasks['A']).parameters) == ['value']
    assert w.tasks['A'](value=3) == 3
    events = []
    assert floe.run(w, events.append)
    assert [(e.event, e.task) for e in events if e.task] == [
        ('task_started', 'A'), ('task_completed', 'A'), ('task_started', 'B'), ('task_completed', 'B')]


def test_marker_is_snapshotted_and_does_not_make_async_valid():
    def work(): pass
    w = floe.Workflow('test', {'A': work}, {'A': set()})
    assert floe.parallel(work) is work and not w.parallel_tasks
    assert 'parallel_tasks' not in w.to_dict()
    @floe.parallel
    async def no(): pass
    with pytest.raises(floe.ValidationError, match='synchronous'):
        floe.Workflow('test', {'A': no}, {'A': set()})


def test_decorated_method_is_eligible_even_without_loader_wrapper():
    class Owner:
        @floe.parallel
        def work(self): pass
    w = floe.Workflow('test', {'A': Owner().work}, {'A': set()})
    assert w.parallel_tasks == {'A'}
    with pytest.raises(floe.ValidationError, match='Python function'):
        floe.parallel(len)


@pytest.mark.parametrize('bad', [True, 0, -1, 1.5, '2'])
def test_invalid_limit(bad):
    with pytest.raises(floe.ValidationError, match='positive integer'):
        schedule({}, limit=bad)


@pytest.mark.parametrize('limit', [1, 2])
def test_capacity_and_join(limit):
    s = schedule({'A': set(), 'B': set(), 'J': {'A', 'B'}}, limit=limit)
    assert admit(s) == (['A'] if limit == 1 else ['A', 'B'])
    if limit == 1:
        s.finish('A', 'succeeded')
        assert admit(s) == ['B']
        s.finish('B', 'succeeded')
    else:
        s.finish('B', 'succeeded')
        assert admit(s) == []
        s.finish('A', 'succeeded')
    assert admit(s) == ['J'] and not s.done
    s.finish('J', 'succeeded')
    assert s.done


def test_exclusive_head_drains_active_and_blocks_later_eligible():
    s = schedule(dict.fromkeys('ABCDE', ()), marked='ABDE', limit=3)
    assert admit(s) == ['A', 'B']
    s.finish('B', 'succeeded')
    assert admit(s) == []
    s.finish('A', 'succeeded')
    assert admit(s) == ['C']
    s.finish('C', 'succeeded')
    assert admit(s) == ['D', 'E']


@pytest.mark.parametrize('scope', ['none', 'task', 'workflow'])
def test_cancellation_matrix_with_queued_root_and_transitive_dependent(scope):
    s = schedule({'A': (), 'B': (), 'Q': (), 'C': ('A',), 'D': ('B',), 'E': ('C',)})
    assert admit(s) == ['A', 'B']
    if scope == 'workflow':
        assert s.cancel_workflow() == ('A', 'B') and s.cancel_workflow() == ()
        s.finish('A', 'cancelled')
        assert admit(s) == []  # Q must not take A's slot
        s.finish('B', 'cancelled')
        assert admit(s) == [] and s.done
        assert s.outcomes == {'A': 'cancelled', 'B': 'cancelled', **dict.fromkeys('QCDE', 'skipped')}
    else:
        if scope == 'task':
            assert s.cancel_task('A') and not s.cancel_task('A')
            assert 'A' not in s.outcomes  # a request is not an observed completion
            s.finish('A', 'cancelled')
        else:
            s.finish('A', 'succeeded')
        assert not s.cancel_task('A')  # cannot retract a terminal outcome
        assert admit(s) == ['Q']  # B still active
        s.finish('Q', 'succeeded')
        assert admit(s) == ([] if scope == 'task' else ['C'])
        s.finish('B', 'succeeded')
        assert admit(s) == ['D']
        s.finish('D', 'succeeded')
        if scope == 'none':
            s.finish('C', 'succeeded')
            assert admit(s) == ['E']
            s.finish('E', 'succeeded')
        assert s.done and s.outcomes['D'] == 'succeeded'
        assert s.outcomes['C'] == s.outcomes['E'] == ('skipped' if scope == 'task' else 'succeeded')
    assert s.cancel_workflow() == ()  # late request cannot alter the run outcome
    assert s.run_outcome == ('succeeded' if scope == 'none' else 'cancelled')


def test_failure_drains_peer_and_blocks_ready_and_later_tasks():
    s = schedule({'A': (), 'B': (), 'queued': (), 'D': ('B',)})
    assert admit(s) == ['A', 'B']
    s.finish('A', 'failed')
    assert admit(s) == [] and not s.done
    s.finish('B', 'succeeded')
    assert admit(s) == [] and s.done
    assert s.outcomes == {'A': 'failed', 'B': 'succeeded', 'queued': 'skipped', 'D': 'skipped'}


def test_unconfirmed_cancel_cannot_commit_success_or_free_a_slot():
    s = schedule({'A': (), 'C': ('A',)})
    assert admit(s) == ['A']
    with pytest.raises(floe.ValidationError):
        s.finish('A', 'cancelled')  # unexplained exit is not explicit cancellation
    assert s.cancel_task('A')
    with pytest.raises(floe.ValidationError):
        s.finish('A', 'succeeded')
    assert not s.done and admit(s) == [] and s.run_outcome is None
    assert s.cancel_workflow() == ()  # A already has a stop request
    s.finish('A', 'failed')  # e.g. cleanup failed
    assert admit(s) == [] and s.outcomes['C'] == 'skipped' and s.run_outcome == 'failed'
    with pytest.raises(floe.ValidationError):
        s.finish('A', 'failed')
