"""Demo: CRUD over systemd unit files using the repo's and installed mydag units.

Reads ~/.config/systemd/user (read-only) and writes only to a temp directory.

    uv run python examples/units_demo.py
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from floe.units import NotOwnedError, ServiceSection, UnitDir

REPO = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "legacy"


def show(title, text):
    print(f"\n--- {title} " + "-" * (60 - len(title)))
    print(text.rstrip())


# 1. READ existing, hand-written units (owner=None: no ownership filter).
installed = UnitDir("~/.config/systemd/user", owner=None)
print(f"{installed!r}: {list(installed)}  (timers.target.wants/ symlink skipped)")
timer = installed["mydag.timer"]
service = installed["mydag.service"]
print(f"mydag.timer   OnCalendar={timer.timer.on_calendar} Persistent={timer.timer.persistent!r}")
print(f"mydag.service ExecStart={service.service.exec_start}")
print(f"repo copies identical to installed: {UnitDir(REPO, owner=None)['mydag.timer'] == timer}")

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for f in REPO.iterdir():  # simulate an installed, enabled user dir
        shutil.copy(f, tmp)
    (tmp / "timers.target.wants").mkdir()
    (tmp / "timers.target.wants" / "mydag.timer").symlink_to(tmp / "mydag.timer")

    units = UnitDir(tmp)  # owner="floe"
    print(f"\nowned units in copy: {list(units)}  (hand-written files are not ours)")
    try:
        units["mydag.service"] = service
    except NotOwnedError as e:
        print(f"overwrite refused: {e}")

    # 2. CREATE: derive floe-owned units from the existing ones.
    units["dag-mydag.service"] = service.model_copy(update={
        "service": service.service.model_copy(update={
            "timeout_start_sec": "30min", "kill_mode": "control-group",
            "syslog_identifier": "dag-mydag",
        }),
    })
    units["dag-mydag.timer"] = timer.model_copy(update={
        "timer": timer.timer.model_copy(update={"unit": "dag-mydag.service"}),
    })
    print(f"owned units after create: {list(units)}")
    show("dag-mydag.service (generated)", (tmp / "dag-mydag.service").read_text())
    show("dag-mydag.timer (generated)", (tmp / "dag-mydag.timer").read_text())

    # 3. UPDATE: frozen models -> copy with changes and assign back.
    t = units["dag-mydag.timer"]
    units["dag-mydag.timer"] = t.model_copy(update={
        "timer": t.timer.model_copy(update={"on_calendar": ["*-*-* 08:00:00"]}),
    })
    print(f"\nupdated OnCalendar: {units['dag-mydag.timer'].timer.on_calendar}")
    print(f"rewrite unchanged -> written: {units.put('dag-mydag.timer', units['dag-mydag.timer'])}")

    # Validation happens at construction.
    try:
        ServiceSection(exec_start=["/bin/date +%F"])
    except Exception as e:
        print(f"validation: {str(e).splitlines()[1].strip()}")

    # systemd itself accepts what we wrote.
    if shutil.which("systemd-analyze"):
        r = subprocess.run(
            ["systemd-analyze", "--user", "verify", *(str(tmp / n) for n in units)],
            capture_output=True, text=True,
        )
        print(f"systemd-analyze verify: {'ok' if r.returncode == 0 else r.stderr.strip()}")

    # 4. DELETE: takes over the hand-written timer, then removes it + its enable link.
    units.put("mydag.timer", timer, force=True)
    del units["mydag.timer"]
    del units["dag-mydag.timer"], units["dag-mydag.service"]
    print(f"\nafter delete: owned={list(units)}, all={list(UnitDir(tmp, owner=None))}, "
          f"wants link exists={(tmp / 'timers.target.wants' / 'mydag.timer').is_symlink()}")
