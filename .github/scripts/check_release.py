"""Check release metadata and write the notes and prerelease flag for CI."""

import os
import re
import tomllib
from pathlib import Path


def main():
    number = r'(0|[1-9][0-9]*)'
    tag = os.environ['RELEASE_TAG']
    match = re.fullmatch(rf'v{number}\.{number}\.{number}(?:-(alpha|beta|rc)\.{number})?', tag)
    if match is None:
        raise SystemExit('Use vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-(alpha|beta|rc).N')
    major, minor, patch, kind, serial = match.groups()
    suffix = '' if kind is None else {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[kind] + serial
    version = f'{major}.{minor}.{patch}{suffix}'
    project = tomllib.loads(Path('pyproject.toml').read_text())['project']
    if project['name'] != 'floe' or project['version'] != version:
        raise SystemExit(f'{tag} requires package version {version}')
    lock = tomllib.loads(Path('uv.lock').read_text())
    packages = [item for item in lock['package'] if item['name'] == 'floe']
    if len(packages) != 1 or packages[0]['version'] != version or packages[0]['source'] != {'editable': '.'}:
        raise SystemExit('Run uv lock: the locked project version does not match')
    heading = re.compile(rf'## \[{re.escape(tag[1:])}\](?: - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})?')
    sections = []
    active = False
    for line in Path('CHANGELOG.md').read_text().splitlines():
        if line.startswith('## '):
            active = heading.fullmatch(line) is not None
            if active:
                sections.append([])
        elif active:
            sections[-1].append(line)
    if len(sections) != 1 or not (notes := '\n'.join(sections[0]).strip()):
        raise SystemExit(f'Add exactly one nonempty CHANGELOG.md section for [{tag[1:]}]')
    Path(os.environ['RUNNER_TEMP'], 'release-notes.md').write_text(notes + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'prerelease={str(kind is not None).lower()}\n')


if __name__ == '__main__':
    main()
