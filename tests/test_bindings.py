import json
from pathlib import Path
import subprocess
import sys

import pytest

import floe
from floe import results


def test_file_join_uses_named_results_not_order_or_stdout(tmp_path):
    flow = tmp_path / 'join.py'
    flow.write_text('''
WORKFLOW = 'join'
def prices():
    print('different from result')
    return {'apple': 7}
def quantities():
    return {'apple': 3}
def join(*, quantities, prices):
    print('total', prices['apple'] * quantities['apple'])
tasks = {'C': join, 'B': quantities, 'A': prices}
dag = {'C': {'B'}, 'B': set(), 'A': set()}
inputs = {'C': {'prices': 'A', 'quantities': 'B'}}
''')
    proc = subprocess.run([sys.executable, '-m', 'floe_cli', 'run', str(flow)],
                          capture_output=True, text=True, timeout=10, check=True)
    assert proc.stderr.splitlines() == ['different from result', 'total 21']
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    completed = [e['task'] for e in events if e['event'] == 'task_completed']
    assert sorted(completed[:-1]) == ['A', 'B'] and completed[-1] == 'C'
    assert events[-1]['status'] == 'succeeded' and 'apple' not in proc.stdout


def test_bound_values_are_snapshots_per_attempt_and_consumer():
    seen = []
    original = {'items': [None]}
    bindings = {'B': {'v': 'A'}, 'C': {'v': 'A'}}

    def b(v):
        seen.append(v['items'].copy())
        v['items'].append(1)
        if len(seen) == 1:
            raise TimeoutError()

    def c(v):
        seen.append(v['items'].copy())

    order_only = {'C': {'B'}, 'B': set(), 'A': set()}
    w = floe.Workflow('test', {'A': lambda: original, 'B': b, 'C': c},
                      order_only, inputs=bindings,
                      retries={'B': floe.RetryPolicy(2, 0, (TimeoutError,))})
    bindings['C']['v'] = 'B'
    assert w.to_dict()['inputs']['C'] == {'v': 'A'}
    assert w.dag['C'] == {'A', 'B'} and order_only['C'] == {'B'} and order_only['B'] == set()
    assert floe.run(w, lambda e: None)
    assert seen == [[None], [None], [None]] and original == {'items': [None]}


def test_definition_reports_bad_bindings_and_mixed_cycle():
    with pytest.raises(floe.ValidationError) as err:
        floe.Workflow('flow', {'A': lambda x, /: x, 'B': lambda: 1},
                      {'A': set(), 'B': {'A'}}, inputs={'A': {'x': 'B'}, 'Z': {'q': 'missing'}})
    for message in ('invalid input signature', 'dependency cycle', 'unknown task', 'invalid input producer'):
        assert message in str(err.value)


@pytest.mark.parametrize('bad', [(1,), {2: 'no'}, float('nan'), float('inf'), object()])
def test_invalid_bound_result_fails_without_retry_or_consumer(bad):
    called = []
    events = []
    w = floe.Workflow('test', {'A': lambda: called.append('A') or bad, 'B': lambda v: called.append('B')},
                      {'B': set(), 'A': set()}, inputs={'B': {'v': 'A'}},
                      retries={'A': floe.RetryPolicy(3, 0, (Exception,))})
    assert not floe.run(w, events.append)
    assert called == ['A']
    assert events[-2].failure_reason == 'non_retryable' and events[-1].status == 'failed'


def test_null_is_present_and_unconsumed_return_is_ignored():
    seen = []
    w = floe.Workflow('test', {'A': lambda: None, 'B': lambda x: seen.append(x) or Path('ignored')},
                      {'A': set(), 'B': set()}, inputs={'B': {'x': 'A'}})
    assert floe.run(w, lambda e: None) and seen == [None]


def test_retention_failure_reported_on_producer_not_consumer(monkeypatch):
    monkeypatch.setattr(results, 'MAX_RETAINED_BYTES', 5)
    calls = []
    events = []
    w = floe.Workflow('test', {'A': lambda: 'a', 'B': lambda: 'b', 'C': lambda **kw: calls.append(kw)},
                      {'A': set(), 'B': {'A'}, 'C': set()}, inputs={'C': {'a': 'A', 'b': 'B'}})
    assert not floe.run(w, events.append) and not calls
    assert (events[-2].task, events[-2].status, events[-1].status) == ('B', 'failed', 'failed')
    assert 'retained byte limit' in events[-2].error


def test_loader_rejects_nonmapping_inputs(tmp_path):
    f = tmp_path / 'bad.py'
    f.write_text("WORKFLOW='test'\ntasks={'A': lambda: 1}\ndag={'A': set()}\ninputs=[]\n")
    with pytest.raises(floe.ValidationError, match='inputs must be a mapping'):
        floe.load(f)


def test_limits_apply_to_encoded_size_and_release_last_use(monkeypatch):
    monkeypatch.setattr(results, 'MAX_RESULT_BYTES', 5)
    monkeypatch.setattr(results, 'MAX_RETAINED_BYTES', 6)
    r = results.RunResults({'B': {'x': 'A', 'y': 'A'}, 'D': {'x': 'B', 'y': 'C'}})
    with pytest.raises(floe.ValidationError, match='per-result'):
        r.complete('A', '\u2603')  # default JSON escaping uses eight bytes
    loop = []; loop.append(loop)
    with pytest.raises(floe.ValidationError, match='cyclic'):
        r.complete('A', loop)
    r.complete('A', 'abc')
    assert r.arguments('B') == {'x': 'abc', 'y': 'abc'}
    r.complete('B', 'def')  # capacity from A is available again
    with pytest.raises(floe.ValidationError, match='retained'):
        r.complete('C', 'g')
