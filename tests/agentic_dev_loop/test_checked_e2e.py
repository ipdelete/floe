from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.agentic_dev_loop import checked_e2e as checked, live_e2e
from tools.agentic_dev_loop.feature_tree import IssueRef
from tools.agentic_dev_loop.pr_agent_review import Comment, ReviewEvidence, require_review


REAL_SUITE = live_e2e._run_suite
FIXTURE_TEST = 'import pytest\n@pytest.mark.systemd_e2e\ndef test_fixture():\n    assert True\n'
REVIEW_TIME = datetime(2026, 11, 20, tzinfo=UTC)


def review_comment(fixture, *, findings=(), **run):
    state = {
        'schema_version': 1,
        'last_run': {'complete': True, 'head_sha': fixture.binding.head_sha, 'kind': 'full',
                     'run_id': 'review-1', **run},
        'findings': list(findings),
    }
    return Comment(1, fixture.review_producer, REVIEW_TIME, REVIEW_TIME,
                   f'<!-- pr-agent-review-state:v1\n{json.dumps(state)}\n-->')


@pytest.fixture
def rig(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', repo, *args], timeout=10).decode().strip()

    git('init', '--quiet')
    (repo / 'test_fixture.py').write_text(FIXTURE_TEST)
    (repo / 'pytest.ini').write_text('[pytest]\nmarkers = systemd_e2e\n')
    (repo / 'conftest.py').write_text('''
def pytest_addoption(parser):
    parser.addoption('--systemd-e2e', action='store_true')
''')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    for path in repo.glob('*.*'):
        if path.is_file():
            (checkout / path.name).write_bytes(path.read_bytes())
    binding = checked.Binding(IssueRef('fixture/floe', 42), 'a' * 40, 'b' * 40,
                              git('rev-parse', 'HEAD'), 'd' * 40)
    fixture = checked.Fixture(binding, repo, checkout, Path(sys.executable),
                              'protected-ci', 'protected-review', 'loop-author')
    ci = checked.Check(binding, 'protected-ci', 'ci-1', 'success')
    review = ReviewEvidence(binding.pr, (review_comment(fixture),))
    evidence = checked.Evidence(binding, True, False, ci, review)
    target = live_e2e.Target(tmp_path / 'home', tmp_path / 'runtime')
    receipt = tmp_path / 'receipt.json'
    trace = []

    def audit(actual):
        assert actual == target
        assert target.dirty.exists()
        assert not receipt.exists()
        trace.append('audit')

    def run(actual, checkout, python, fd, selection, *, checkout_fd):
        assert (actual, checkout, python, selection) == (target, fixture.checkout, fixture.python, None)
        assert Path(f'/proc/self/fd/{checkout_fd}/test_fixture.py').read_text() == FIXTURE_TEST
        assert target.dirty.exists()
        assert not receipt.exists()
        trace.append('suite')
        return 0

    monkeypatch.setattr(live_e2e, 'verify_clean', audit)
    monkeypatch.setattr(live_e2e, '_run_suite', run)
    return target, fixture, evidence, receipt, trace


def test_exact_success_is_recorded_after_locked_cleanup(rig, monkeypatch):
    target, fixture, evidence, receipt, trace = rig
    write_receipt = checked._write_receipt

    def write(path, result):
        assert trace == ['audit', 'suite', 'audit']
        assert not target.dirty.exists()
        with pytest.raises(live_e2e.Blocked, match='busy'):
            with live_e2e._locked(target):
                pytest.fail('manager lease must still be held at publication')
        write_receipt(path, result)

    monkeypatch.setattr(checked, '_write_receipt', write)

    def read(pr):
        assert pr == fixture.binding.pr
        assert not receipt.exists()
        with pytest.raises(live_e2e.Blocked, match='busy'):
            with live_e2e._locked(target):
                pytest.fail('manager lease must still be held')
        if trace == ['audit']:
            assert target.dirty.exists()
        elif trace:
            assert trace == ['audit', 'suite', 'audit']
            assert not target.dirty.exists()
        return evidence

    attestation = checked.validate_fixture(target, fixture, read, receipt)
    assert trace == ['audit', 'suite', 'audit']
    assert attestation.matches(fixture.binding)
    assert json.loads(receipt.read_text()) == {
        'kind': 'supervised-fixture-e2e', 'binding': asdict(fixture.binding),
        'ci_run': 'ci-1',
        'review_run': require_review(evidence.review, fixture.binding.pr, fixture.binding.head_sha,
                                     fixture.review_producer, fixture.loop_author).run_id,
    }
    assert receipt.stat().st_mode & 0o777 == 0o600
    for field in ('pr', 'head_sha', 'base_sha', 'merge_sha', 'adr_revision'):
        changed = IssueRef('another/floe', 42) if field == 'pr' else 'e' * 40
        assert not attestation.matches(replace(fixture.binding, **{field: changed}))
    with pytest.raises(live_e2e.Blocked, match='receipt already exists'):
        checked.validate_fixture(target, fixture, read, receipt)
    assert trace == ['audit', 'suite', 'audit']


@pytest.mark.parametrize('fault', ['missing', 'unknown', 'draft', 'pending-redesign', 'unknown-redesign',
                                   'missing-ci', 'missing-review', 'pending-ci', 'pending-review',
                                   'stale-ci', 'stale-review', 'spoofed-ci', 'spoofed-review',
                                   'unknown-findings', 'pending-finding', 'unknown-commit'])
def test_unverified_evidence_never_starts_e2e(rig, fault):
    target, fixture, e, receipt, trace = rig
    stale = replace(e.binding, base_sha='e' * 40)
    review = e.review.comments[0]

    def with_review(comment):
        return replace(e, review=ReviewEvidence(fixture.binding.pr, (comment,)))

    if fault == 'unknown-commit':
        expected = replace(e.binding, merge_sha='f' * 40)
        fixture = replace(fixture, binding=expected)
        e = replace(e, binding=expected, ci=replace(e.ci, binding=expected))
    else:
        e = {
            'missing': None,
            'unknown': replace(e, binding=None),
            'draft': replace(e, open_nondraft=False),
            'pending-redesign': replace(e, redesign_pending=True),
            'unknown-redesign': replace(e, redesign_pending=None),
            'missing-ci': replace(e, ci=None),
            'missing-review': replace(e, review=None),
            'pending-ci': replace(e, ci=replace(e.ci, status='pending')),
            'pending-review': with_review(review_comment(fixture, complete=False, kind='partial')),
            'stale-ci': replace(e, ci=replace(e.ci, binding=stale)),
            'stale-review': with_review(review_comment(fixture, head_sha='e' * 40)),
            'spoofed-ci': replace(e, ci=replace(e.ci, producer='candidate-ci')),
            'spoofed-review': with_review(replace(review, author='PR-Agent')),
            'unknown-findings': with_review(replace(review, body=review.body.replace('[]', 'null'))),
            'pending-finding': with_review(review_comment(fixture, findings=('pending',))),
        }[fault]
    with pytest.raises(live_e2e.Blocked):
        checked.validate_fixture(target, fixture, lambda _: e, receipt)
    assert trace == []
    assert not receipt.exists()


@pytest.mark.parametrize('fault', ['modified', 'extra', 'symlink', 'mode', 'replaced-before-launch',
                                   'replaced-after-launch', 'test-failed', 'interrupted', 'cleanup', 'redesign'])
def test_no_attestation_for_wrong_checkout_or_unclean_run(rig, monkeypatch, fault):
    target, fixture, evidence, receipt, trace = rig
    suite = live_e2e._run_suite
    audit = live_e2e.verify_clean
    source = fixture.checkout / 'test_fixture.py'

    def swap():
        fixture.checkout.rename(fixture.checkout.with_name('original'))
        fixture.checkout.mkdir()
        for name in ('test_fixture.py', 'pytest.ini', 'conftest.py'):
            (fixture.checkout / name).write_bytes((fixture.repo / name).read_bytes())

    def run(*args, **kwargs):
        status = suite(*args, **kwargs)
        if fault == 'interrupted':
            raise KeyboardInterrupt
        if fault == 'test-failed':
            return 1
        if fault == 'replaced-after-launch':
            swap()
        return status

    def check(actual):
        audit(actual)
        if fault == 'cleanup' and trace.count('audit') == 2:
            raise live_e2e.Blocked('cleanup audit found residue')
        if fault == 'replaced-before-launch':
            swap()

    if fault == 'modified':
        source.write_text('# wrong bytes\n')
    elif fault == 'extra':
        source.with_name('sitecustomize.py').touch()
    elif fault == 'symlink':
        source.unlink()
        source.symlink_to(fixture.repo / source.name)
    elif fault == 'mode':
        source.chmod(0o755)
    monkeypatch.setattr(live_e2e, 'verify_clean', check)
    monkeypatch.setattr(live_e2e, '_run_suite', run)

    def read(_):
        return replace(evidence, redesign_pending=True) if fault == 'redesign' and 'suite' in trace else evidence

    with pytest.raises(KeyboardInterrupt if fault == 'interrupted' else live_e2e.Blocked):
        checked.validate_fixture(target, fixture, read, receipt)
    assert not receipt.exists()
    if fault in ('modified', 'extra', 'symlink', 'mode'):
        assert trace == []
    elif fault == 'replaced-before-launch':
        assert trace == ['audit']
    else:
        assert trace.count('suite') == 1
    assert target.dirty.exists() == (fault in ('replaced-before-launch', 'test-failed', 'interrupted', 'cleanup'))


def test_active_pr_agent_finding_blocks_before_suite(rig):
    target, fixture, evidence, receipt, trace = rig
    review = review_comment(fixture, findings=({'finding_id': 'abc', 'state': 'ACTIVE',
                                                'path': 'foo.py', 'body': 'Possible issue'},))
    pending = replace(evidence, review=ReviewEvidence(fixture.binding.pr, (review,)))
    with pytest.raises(live_e2e.Blocked, match='abc'):
        checked.validate_fixture(target, fixture, lambda _: pending, receipt)
    assert trace == [] and not receipt.exists()

    later = REVIEW_TIME + timedelta(seconds=2)
    disposition = Comment(2, fixture.loop_author, later, later, '- abc: fixed — restored the check')
    clear = replace(evidence, review=ReviewEvidence(fixture.binding.pr, (review, disposition)))
    attestation = checked.validate_fixture(target, fixture, lambda _: clear, receipt)
    assert attestation.review_run == require_review(clear.review, fixture.binding.pr, fixture.binding.head_sha,
                                                    fixture.review_producer, fixture.loop_author).run_id
    assert trace == ['audit', 'suite', 'audit'] and receipt.is_file()


def test_same_head_review_update_during_suite_prevents_receipt(rig):
    target, fixture, evidence, receipt, trace = rig
    updated = replace(evidence.review.comments[0], updated_at=REVIEW_TIME + timedelta(seconds=1))

    def read(_):
        if 'suite' in trace:
            return replace(evidence, review=replace(evidence.review, comments=(updated,)))
        return evidence

    with pytest.raises(live_e2e.Blocked, match='review evidence changed'):
        checked.validate_fixture(target, fixture, read, receipt)
    assert trace == ['audit', 'suite', 'audit'] and not receipt.exists()


def test_redesign_becoming_pending_during_initial_audit_blocks_launch(rig):
    target, fixture, evidence, receipt, trace = rig

    def read(_):
        return replace(evidence, redesign_pending=True) if trace else evidence

    with pytest.raises(live_e2e.Blocked, match='redesign'):
        checked.validate_fixture(target, fixture, read, receipt)
    assert trace == ['audit']
    assert not receipt.exists()


def test_real_child_enters_pinned_directory_even_if_path_is_swapped_at_spawn(rig, monkeypatch, capfd):
    target, fixture, evidence, receipt, trace = rig
    popen = subprocess.Popen

    def swap_at_handoff(args, *pos, **kwargs):
        if '_checked_suite' in args:
            fixture.checkout.rename(fixture.checkout.with_name('original'))
            fixture.checkout.mkdir()
            (fixture.checkout / 'conftest.py').write_text('raise RuntimeError("wrong checkout")')
        return popen(args, *pos, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', swap_at_handoff)
    monkeypatch.setattr(live_e2e, '_run_suite', REAL_SUITE)
    with pytest.raises(live_e2e.Blocked, match='checkout was replaced'):
        checked.validate_fixture(target, fixture, lambda _: evidence, receipt)
    assert '1 passed' in capfd.readouterr().out
    assert trace == ['audit', 'audit']
    assert not target.dirty.exists()
    assert not receipt.exists()


def test_unattended_candidate_entry_does_not_exist():
    with pytest.raises(SystemExit) as exc:
        checked.main(['candidate'])
    assert exc.value.code == 2


def test_desktop_demo_rejects_non_allowlisted_pin(rig, monkeypatch, capsys):
    _, fixture, _, receipt, _ = rig
    monkeypatch.setattr(checked, 'validate_fixture', lambda *args: pytest.fail('must not launch'))
    args = ['fixture', '--repo', str(fixture.repo), '--checkout', str(fixture.checkout),
            '--python', str(fixture.python), '--receipt', str(receipt), '--pin', fixture.binding.merge_sha]
    with pytest.raises(SystemExit) as exc:
        checked.main(args)
    assert exc.value.code == 2
    assert 'reviewed fixture pin' in capsys.readouterr().err
