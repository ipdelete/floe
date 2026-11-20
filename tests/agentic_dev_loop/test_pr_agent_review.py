from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from subprocess import CalledProcessError

import pytest

from tools.agentic_dev_loop import pr_agent_review as review
from tools.agentic_dev_loop.feature_tree import IssueRef


PR = IssueRef('fixture/floe', 42)
HEAD, OLD = 'a' * 40, 'b' * 40
T = datetime(2026, 11, 20, tzinfo=UTC)
REVIEWER, LOOP = 'reviewer', 'maintainer'


def comment(body, *, id=1, author=REVIEWER, created=T, updated=None):
    return review.Comment(id, author, created, updated if updated is not None else created, body)


def persistent(*, findings=(('abc', 'ACTIVE'), ('def', 'RESOLVED')), **run):
    state = {
        'schema_version': 1,
        'last_run': {'complete': True, 'head_sha': HEAD, 'kind': 'full', 'run_id': 'commit-url',
                     'excluded_files': [], **run},
        'findings': [{'finding_id': id, 'state': status, 'path': 'foo.py', 'body': 'Possible issue'}
                     for id, status in findings],
    }
    return comment('## PR Reviewer Guide 🔍\n\n<!-- pr-agent:review:full -->\n\n'
                   f'<!-- pr-agent-review-state:v1\n{json.dumps(state)}\n-->')


def disposition(body='- abc: not applicable — intentional edge case', **kwargs):
    return comment(body, **{'id': 2, 'author': LOOP, 'created': T + timedelta(seconds=2), **kwargs})


def check(*comments, standalone=None):
    return review.require_review(review.ReviewEvidence(PR, comments, standalone), PR, HEAD, REVIEWER, LOOP)


@pytest.mark.parametrize('line', ['- abc: fixed — 1234abcd', '- abc: not applicable — deliberate'])
def test_active_id_needs_disposition_but_resolved_id_does_not(line):
    run = check(disposition(line), persistent())
    assert run.active_ids == ('abc',)
    assert check(persistent(findings=(('def', 'RESOLVED'),))).active_ids == ()


@pytest.mark.parametrize(('kwargs', 'message'), [
    ({'author': 'stranger'}, 'abc'),
    ({'created': T}, 'abc'),
    ({'created': T - timedelta(seconds=1), 'updated': T + timedelta(seconds=3)}, 'abc'),
    ({'body': '- abc: not applicable —   '}, 'abc'),
    ({'body': '- abc: ignored — fine'}, 'abc'),
    ({'body': '- xyz: fixed — unrelated'}, 'abc'),
    ({'body': '> - abc: fixed — quotation'}, 'abc'),
    ({'body': '```text\n- abc: fixed — example\n```'}, 'abc'),
])
def test_invalid_dispositions_do_not_count(kwargs, message):
    with pytest.raises(review.ReviewBlocked, match=message):
        check(persistent(), disposition(**kwargs))


def test_mixed_disposition_body_is_ignored_with_format_hint():
    with pytest.raises(review.ReviewBlocked, match='abc.*dedicated comment'):
        check(persistent(), disposition('Notes from review:\n- abc: fixed — repaired'))


def test_all_active_findings_must_be_accounted_for():
    current = persistent(findings=(('abc', 'ACTIVE'), ('def', 'ACTIVE')))
    with pytest.raises(review.ReviewBlocked, match='def'):
        check(current, disposition())
    assert check(current, disposition('- abc: fixed — repaired\n- def: not applicable — reason')).active_ids == (
        'abc', 'def')


def test_a_same_head_comment_update_invalidates_earlier_disposition_and_run_id():
    current = persistent()
    run = check(current, disposition())
    updated = replace(current, updated_at=T + timedelta(seconds=3))
    with pytest.raises(review.ReviewBlocked, match='abc'):
        check(updated, disposition())
    assert check(updated, disposition(created=T + timedelta(seconds=4))).run_id != run.run_id


@pytest.mark.parametrize(('make_comments', 'message'), [
    (lambda: (), 'no PR-Agent review'),
    (lambda: (replace(persistent(), author='stranger'),), 'no PR-Agent review'),
    (lambda: (persistent(complete=False, kind='partial'),), 'incomplete'),
    (lambda: (persistent(head_sha=OLD),), 'different head'),
    (lambda: (persistent(findings=(('abc', 'ACTIVE'), ('abc', 'RESOLVED'))),), 'malformed'),
    (lambda: (persistent(findings=(('abc', []),)),), 'malformed'),
    (lambda: (comment('<!-- pr-agent-review-state:v1\nnot json\n-->'),), 'malformed'),
    (lambda: (comment('<!-- pr-agent-review-state:v1\n{"schema_version":1,"schema_version":1}\n-->'),), 'malformed'),
    (lambda: (comment('<!-- pr-agent-review-state:v2\n{}\n-->'),), 'malformed'),
    (lambda: (replace(persistent(), body=persistent().body.replace('Possible issue', '--!>')),), 'malformed'),
    (lambda: (comment('## PR Reviewer Guide 🔍\n\n<!-- pr-agent:review:full -->'),), 'malformed'),
    (lambda: (persistent(), comment('<!-- pr-agent-review-state:v1\n{', id=3,
                                    created=T + timedelta(seconds=4))), 'malformed'),
    (lambda: (persistent(), replace(persistent(), id=3)), 'ambiguous'),
])
def test_missing_stale_incomplete_or_malformed_review_blocks(make_comments, message):
    with pytest.raises(review.ReviewBlocked, match=message):
        check(*make_comments(), disposition())


def test_review_snapshot_must_belong_to_requested_pr():
    evidence = review.ReviewEvidence(IssueRef(PR.repo, 43), (persistent(), disposition()))
    with pytest.raises(review.ReviewBlocked, match='PR'):
        review.require_review(evidence, PR, HEAD, REVIEWER, LOOP)


def test_standalone_requires_exact_run_evidence_and_later_dispositions():
    standalone = comment('## Standalone PR Review\n\n_PR-Agent could not safely update the persistent review. '
                         'This standalone result will not replace the canonical review._\n\n## PR Reviewer Guide 🔍',
                         id=3, created=T + timedelta(seconds=1))
    receipt = review.StandaloneRun(standalone, HEAD, True, ('standalone-1', 'standalone-2'))
    with pytest.raises(review.ReviewBlocked, match='standalone.*evidence'):
        check(standalone, disposition())
    with pytest.raises(review.ReviewBlocked, match='standalone-2'):
        check(standalone, disposition('- standalone-1: fixed — note'), standalone=receipt)
    dispositions = disposition('- standalone-1: fixed — note\n- standalone-2: not applicable — reason')
    assert check(standalone, dispositions, standalone=receipt).active_ids == ('standalone-1', 'standalone-2')
    with pytest.raises(review.ReviewBlocked, match='different head'):
        check(standalone, dispositions, standalone=replace(receipt, head_sha=OLD))
    with pytest.raises(review.ReviewBlocked, match='incomplete'):
        check(standalone, dispositions, standalone=replace(receipt, complete=False))
    with pytest.raises(review.ReviewBlocked, match='standalone.*evidence'):
        check(replace(standalone, body=standalone.body + '\nedit'), dispositions, standalone=receipt)
    with pytest.raises(review.ReviewBlocked, match='abc'):
        check(persistent(), standalone, dispositions, standalone=receipt)
    assert check(persistent(), standalone, replace(dispositions, body=dispositions.body + '\n- abc: fixed — note'),
                 standalone=receipt).active_ids == ('abc', 'standalone-1', 'standalone-2')


def test_read_only_command_pages_comments_and_checks_head(monkeypatch, capsys):
    raw_comments = [{
        'id': c.id, 'body': c.body, 'user': {'login': c.author},
        'created_at': c.created_at.isoformat(), 'updated_at': c.updated_at.isoformat(),
    } for c in (persistent(), disposition())]
    seen = []

    def gh(args, **kwargs):
        assert args[1:4] == ['api', '--method', 'GET']
        assert kwargs['timeout'] > 0
        seen.append(args[4:])
        if 'pulls/42' in args[4]:
            return json.dumps({'head': {'sha': HEAD}})
        assert args[4:] == ['repos/fixture/floe/issues/42/comments?per_page=100', '--paginate']
        return json.dumps([raw_comments[0]]) + '\n' + json.dumps([raw_comments[1]])

    args = [PR.repo, str(PR.number), '--head', HEAD, '--review-author', REVIEWER, '--loop-author', LOOP]
    monkeypatch.setattr(review.subprocess, 'check_output', gh)
    assert review.main(args) == 0
    assert len(seen) == 3 and seen[0] == seen[2]
    assert HEAD in capsys.readouterr().out

    def moved(args, **kwargs):
        if len(seen) == 5:
            return json.dumps({'head': {'sha': OLD}})
        return gh(args, **kwargs)

    monkeypatch.setattr(review.subprocess, 'check_output', moved)
    assert review.main(args) == 2
    assert 'head' in capsys.readouterr().err

    def unavailable(*args, **kwargs):
        raise CalledProcessError(1, args[0])

    monkeypatch.setattr(review.subprocess, 'check_output', unavailable)
    assert review.main(args) == 2
    assert 'could not read' in capsys.readouterr().err
