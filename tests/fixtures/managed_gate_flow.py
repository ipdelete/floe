"""Controlled definition, used only inside a dedicated transient test service."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

WORKFLOW = 'gate_probe'


def wait_for(name):
    end = time.monotonic() + 10
    while not Path(name).exists():
        if time.monotonic() >= end:
            raise TimeoutError(name)
        time.sleep(.01)


def producer():
    # New session and no inherited output writer: group population, not EOF,
    # must gate this task after the direct worker exits.
    leaf = subprocess.Popen([sys.executable, '-c', '''
import os, time
from pathlib import Path
Path('leaf.ready').touch()
end = time.monotonic() + 12
while not Path('leaf.release').exists():
    if time.monotonic() >= end: os._exit(3)
    time.sleep(.01)
'''], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for('leaf.ready')
    Path('members.tmp').write_text(json.dumps([os.getpid(), leaf.pid, Path('/proc/self/cgroup').read_text()]))
    Path('members.tmp').rename('members')
    wait_for('worker.release')
    return {'price': 17}


def consumer(*, item):
    Path('received').write_text(str(item['price']))


tasks = {'A': producer, 'C': consumer}
dag = {'A': set(), 'C': set()}
inputs = {'C': {'item': 'A'}}
