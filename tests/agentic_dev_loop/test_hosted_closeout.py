import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_closeout import SLUG, TAG, freeze, inputs  # noqa: F401
from test_readiness import FEATURE

TRUSTED_ROOT = Path(__file__).resolve().parents[2]
SEED = b'npm_' + b'7Z2k9TyHF4g0QWcMfb3EVNnPau1Jd8zm2XXq'


def git(repo, *args):
    return subprocess.check_output(['/usr/bin/git', '-C', repo, *args]).decode().strip()


@pytest.mark.parametrize('problem', [None, 'seeded', 'extra', 'symlink', 'wrong-base', 'unverified-scanner',
                                     'extra-empty-tree'])
def test_hosted_command_reads_exact_git_delta_without_running_candidate(tmp_path, inputs, problem):
    scanner = os.environ.get('FLOE_TEST_GITLEAKS')
    if not scanner and problem in (None, 'seeded'):
        pytest.skip('set FLOE_TEST_GITLEAKS to exercise the pinned scanner integration')
    repo = tmp_path / 'objects'
    repo.mkdir()
    git(repo, 'init', '-q', '--object-format=sha1')
    git(repo, 'config', 'user.email', 'synthetic@example.invalid')
    git(repo, 'config', 'user.name', 'Synthetic fixture')
    git(repo, 'commit', '-qm', 'previous', '--allow-empty')
    previous = git(repo, 'rev-parse', 'HEAD')
    # Candidate-controlled policy already on the base isn't part of the diff and must not run.
    fake = repo / 'tools' / 'agentic_dev_loop' / 'hosted_closeout.py'
    fake.parent.mkdir(parents=True)
    fake.write_text('raise RuntimeError("candidate code executed")\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    frozen = freeze(inputs)
    artifact = repo / '.agents' / 'engineering' / TAG
    artifact.mkdir(parents=True)
    (artifact / 'index.json').write_bytes(frozen.index)
    summary = artifact / 'summary.md'
    if problem == 'symlink':
        summary.symlink_to('index.json')
    else:
        summary.write_bytes(frozen.summary if problem != 'seeded' else SEED + b' <!-- gitleaks:allow -->\n')
    if problem == 'extra':
        (artifact / 'anything-else').write_text('unvetted')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'candidate')
    head = git(repo, 'rev-parse', 'HEAD')
    if problem == 'extra-empty-tree':
        tree = git(repo, 'rev-parse', f'{head}^{{tree}}')
        empty = subprocess.check_output(['/usr/bin/git', '-C', repo, 'mktree'], input=b'').strip()
        entry = b'040000 tree ' + empty + b'\tunexpected\n'
        entries = subprocess.check_output(['/usr/bin/git', '-C', repo, 'ls-tree', tree])
        tree = subprocess.check_output(['/usr/bin/git', '-C', repo, 'mktree'], input=entries + entry).decode().strip()
        head = git(repo, 'commit-tree', tree, '-p', base, '-m', 'candidate with extra tree entry')
    # Use the actual command, launched from independent tooling with a poisoned PYTHONPATH.
    result = subprocess.run(
        [sys.executable, '-E', '-s', '-B', '-m', 'tools.agentic_dev_loop.hosted_closeout',
         '--repo', str(repo), '--repository', FEATURE.repo, '--feature', str(FEATURE.number), '--slug', SLUG,
         '--base', previous if problem == 'wrong-base' else base, '--head', head,
         '--scanner', str(tmp_path / 'missing') if problem == 'unverified-scanner' or not scanner else scanner],
        cwd=TRUSTED_ROOT, env={**os.environ, 'PYTHONPATH': str(repo)}, capture_output=True, timeout=30,
    )
    assert b'Traceback' not in result.stderr and b'No module named' not in result.stderr
    if problem:
        assert result.returncode == 1 and not result.stdout
        assert SEED not in result.stderr
        assert result.stderr == {
            'seeded': b'scanner finding\n', 'unverified-scanner': b'scanner unverified\n',
        }.get(problem, b'invalid candidate\n')
    else:
        assert result.returncode == 0 and not result.stderr
        receipt = json.loads(result.stdout)
        assert (receipt['base_sha'], receipt['head_sha']) == (base, head)
        assert len(receipt['delta_sha256']) == 64
        assert set(receipt) == {'base_sha', 'head_sha', 'delta_sha256'}
