"""Example workflow definition: data only, no runner code.

    floe run workflow.py        # run now, in this process
    floe install workflow.py    # schedule with systemd
"""

import sys

WORKFLOW = "mydag"
SCHEDULE = ["hourly"]  # OnCalendar= expressions, see systemd.time(7)


def fetch():
    print("Fetching data", file=sys.stderr)


def transform():
    print("Transforming data", file=sys.stderr)


def publish():
    print("Publishing data", file=sys.stderr)


tasks = {
    "fetch": fetch,
    "transform": transform,
    "publish": publish,
}

dag = {
    "fetch": set(),
    "transform": {"fetch"},
    "publish": {"transform"},
}
