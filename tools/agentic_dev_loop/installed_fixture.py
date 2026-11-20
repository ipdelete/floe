"""Controlled worker for the leased installed-lifetime test, not a Pi adapter."""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .feature_tree import FeatureTree, IssueRef, Phase
from .launcher import (
    AttemptKey, Launcher, Reservation, StartAuthority, StartRequest, WorkerState,
    WorkerUnavailable, initialize_ledger,
)
from .readiness import (
    AdrAtMerge, Backlog, DesignEvidence, FeatureEvidence, IssueState, Policy,
    PullRequest, PullRequestState, Role, StoryEvidence,
)
from .tick import AttemptInput, Cooldowns, FeatureScope, Observation, Outcome, Tick, TickLimits


_REVISION = "a" * 40
REQUEST = StartRequest(AttemptKey(100, 101, 103, Role.BUILDER, 1), _REVISION, b"lifetime fixture")
_FEATURE = IssueRef("example/floe", 101)
_POLICY = Policy(maintainer="keeper", adr_path="design.md")

# Fixed payload, never constructed from an issue or from the broken checkout.
_WORKER = '''
import json, os, sys, time
from pathlib import Path
state = Path(sys.argv[1])
run_id = sys.argv[2]
cgroup = Path("/proc/self/cgroup").read_text()
with (state / "launches").open("a") as log:
    print(run_id, file=log)
for sequence in range(1200):
    temp = state / "heartbeat.tmp"
    temp.write_text(json.dumps(dict(run_id=run_id, pid=os.getpid(), cgroup=cgroup, sequence=sequence)))
    temp.replace(state / "heartbeat.json")
    time.sleep(0.1)
'''


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(path)


class FixtureManager:
    """Only a generated transient unit per reservation, with no external effects.

    Description binds run and session in the authorized test manager; it is not
    authentication against a hostile same-UID caller. Missing without a verified
    stop is ambiguous. Unexpected exits do not produce completion evidence.
    """

    def __init__(self, python: Path, state: Path):
        self.python = python
        self.state = state

    @staticmethod
    def unit_name(run_id: str) -> str:
        if str(UUID(run_id)) != run_id:
            raise ValueError("expected a canonical fixture run UUID")
        # The launcher keeps its ordinary logical ID. Translate it to the
        # reserved E2E namespace at the real-manager boundary only.
        return f"floe-e2e-worker-{run_id}.service"

    @staticmethod
    def _description(reservation: Reservation) -> str:
        return f"Floe E2E fixture {reservation.run_id} {reservation.session_id}"

    def _systemctl(self, *args, check=True, timeout=5):
        try:
            return subprocess.run(["/usr/bin/systemctl", "--user", *args], check=check,
                                  text=True, capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkerUnavailable(f"fixture systemctl {args[0]} failed") from exc

    def _show(self, unit: str) -> dict[str, str]:
        result = self._systemctl(
            "show", "--no-pager", "-p", "LoadState,Transient,Description,ActiveState,ControlGroup,Job",
            unit, check=False,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode and not (result.returncode == 1 and self._absent(values)):
            raise WorkerUnavailable("fixture unit query failed")
        return values

    @staticmethod
    def _absent(unit: dict[str, str]) -> bool:
        return (unit.get("LoadState") == "not-found" and unit.get("ActiveState") == "inactive"
                and unit.get("Job", "") in ("", "0") and not unit.get("ControlGroup"))

    def _owns(self, unit: dict[str, str], reservation: Reservation) -> bool:
        return (unit.get("LoadState") == "loaded" and unit.get("Transient") == "yes"
                and unit.get("Description") == self._description(reservation))

    def _stop_receipt(self, reservation: Reservation) -> Path:
        return self.state / f"stopped-{reservation.run_id}.json"

    def start(self, reservation: Reservation, request: StartRequest) -> None:
        unit = self.unit_name(reservation.run_id)
        if request != REQUEST or not self._absent(self._show(unit)):
            raise WorkerUnavailable("not a new fixture reservation")
        try:
            subprocess.run([
                "/usr/bin/systemd-run", "--user", "--no-ask-password", "--collect",
                "--service-type=exec", "--expand-environment=no", f"--unit={unit}",
                f"--description={self._description(reservation)}", f"--working-directory={self.state}",
                "-p", "RuntimeMaxSec=120s", "-p", "TimeoutStopSec=3s", "-p", "KillMode=control-group",
                str(self.python), "-I", "-c", _WORKER, str(self.state), reservation.run_id,
            ], check=True, text=True, capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkerUnavailable("fixture dispatch not confirmed") from exc

    def inspect(self, reservation: Reservation) -> WorkerState:
        unit = self._show(self.unit_name(reservation.run_id))
        if self._absent(unit):
            try:
                receipt = json.loads(self._stop_receipt(reservation).read_text())
            except FileNotFoundError:
                return WorkerState.MISSING
            except (OSError, ValueError) as exc:
                raise WorkerUnavailable("cannot read fixture stop receipt") from exc
            if receipt != {"session_id": reservation.session_id}:
                raise WorkerUnavailable("fixture stop receipt does not match the reservation")
            return WorkerState.CLEAN
        if not self._owns(unit, reservation):
            return WorkerState.FOREIGN
        if unit.get("ActiveState") in ("active", "activating", "deactivating"):
            return WorkerState.RUNNING
        raise WorkerUnavailable("owned fixture unit has no verified outcome")

    def stop(self, reservation: Reservation) -> None:
        name = self.unit_name(reservation.run_id)
        unit = self._show(name)
        if not self._owns(unit, reservation):
            raise WorkerUnavailable("refusing to stop a unit without fixture ownership")
        group = Path(unit.get("ControlGroup", ""))
        if not group.is_absolute() or group.name != name or ".." in group.parts:
            raise WorkerUnavailable("fixture unit has no verifiable cgroup")
        events_file = Path("/sys/fs/cgroup") / group.relative_to("/") / "cgroup.events"
        try:
            events_file.stat()  # Confirm the unit's cgroup is visible before interpreting its removal.
        except OSError as exc:
            raise WorkerUnavailable("cannot locate the fixture cgroup") from exc
        # Recheck after the filesystem probe, immediately before the mutation.
        # systemctl cannot condition its stop on ownership; the lease excludes
        # cooperating callers, not unrelated same-UID manager operations.
        current = self._show(name)
        if not self._owns(current, reservation) or current.get("ControlGroup") != str(group):
            raise WorkerUnavailable("fixture ownership changed before stop")
        self._systemctl("stop", name, timeout=10)
        # A successful stop plus confirmed absence is evidence of cleanup, not
        # just a name that happens to be missing on the next launcher call.
        deadline = time.monotonic() + 5
        while not self._absent(self._show(name)):
            if time.monotonic() >= deadline:
                raise WorkerUnavailable("stopped fixture unit was not collected")
            time.sleep(0.05)
        try:
            events = events_file.read_text().splitlines()
        except FileNotFoundError:
            pass  # The cgroup was removed after the confirmed stop.
        except OSError as exc:
            raise WorkerUnavailable("cannot verify the stopped fixture cgroup") from exc
        else:
            if "populated 0" not in events:
                raise WorkerUnavailable("stopped fixture cgroup is still populated")
        try:
            _atomic_json(self._stop_receipt(reservation), {"session_id": reservation.session_id})
        except OSError as exc:
            raise WorkerUnavailable("cannot record verified fixture stop") from exc

    def effects_reconciled(self, reservation: Reservation) -> bool:
        return True  # No GitHub, model, or side effects outside private fixture files.


class _Issues:
    def __init__(self):
        self.note = None

    def read(self, scope, *, deadline):
        approval = PullRequest(3, _FEATURE.repo, "master", (IssueRef(_FEATURE.repo, 102),),
                               PullRequestState.MERGED, squash_sha=_REVISION, squash_verified=True,
                               merged_by=_POLICY.maintainer)
        evidence = FeatureEvidence(
            FeatureTree({102: Phase.DESIGN, 103: Phase.STORY}),
            {102: DesignEvidence(IssueState.CLOSED, False, None, (approval,),
                                 AdrAtMerge(_POLICY.adr_path, _REVISION, "100644", changed_in_pr=True)),
             103: StoryEvidence(IssueState.OPEN, False, _REVISION, (), None, ())},
            IssueState.OPEN, False, False, backlogs=(Backlog(_REVISION, (103,)),), revision_requests=(),
        )
        return Observation(evidence, {103: AttemptInput(REQUEST.key.issue_id, REQUEST.input_snapshot, self.note)})

    def mirror_if_missing(self, issue, note, *, deadline):
        if self.note is not None:
            return False
        self.note = note
        return True


@dataclass(frozen=True)
class _Admission:
    launcher: Launcher
    authority: StartAuthority
    bucket = "fixture-launcher"

    def start_or_get(self, request, *, deadline):
        return self.launcher.start_or_get(self.authority, request)


@dataclass(frozen=True)
class Fixture:
    name: str
    python: Path
    state: Path

    def __post_init__(self):
        if not re.fullmatch(r"floe-e2e-tick-[0-9a-f]{32}", self.name):
            raise ValueError("expected a generated Floe E2E fixture name")
        if not self.python.is_absolute() or not self.state.is_absolute():
            raise ValueError("fixture requires absolute paths")

    def initialize(self):
        self.state.mkdir(mode=0o700)
        initialize_ledger(self.state / "ledger")

    def manager(self) -> FixtureManager:
        return FixtureManager(self.python, self.state)

    def launcher(self, manager=None) -> tuple[Launcher, StartAuthority]:
        authority = StartAuthority()
        return Launcher(self.state / "ledger", authority, manager if manager is not None else self.manager()), authority

    def admit(self, launcher: Launcher, authority: StartAuthority) -> Reservation:
        tick = Tick(_Issues(), _Admission(launcher, authority), Cooldowns(),
                    limits=TickLimits(tick_seconds=20, call_seconds=20))
        [result] = tick.reconcile([FeatureScope(_FEATURE, REQUEST.key.repo_id, REQUEST.key.feature_id,
                                                _POLICY, "fixture-read", "fixture-write")])
        if result.outcome is not Outcome.MIRRORED or result.reservation is None:
            raise RuntimeError(f"fixture admission not confirmed: {result.outcome}")
        return result.reservation

    def task(self, *, timeout_case: bool):
        def reconcile():
            invocation = os.environ["INVOCATION_ID"]
            if not re.fullmatch(r"[0-9a-f]{32}", invocation):
                raise ValueError("the fixture must run as an installed tick")
            reservation = self.admit(*self.launcher())
            release = self.state / "release"
            child = None
            try:
                if timeout_case and not release.exists():
                    # Intentionally stays in the tick cgroup as a negative control.
                    child = subprocess.Popen([str(self.python), "-I", "-c", "import time; time.sleep(120)"])
                _atomic_json(self.state / f"tick-{invocation}.json", {
                    "invocation": invocation, "pid": os.getpid(),
                    "child_pid": child.pid if child else None,
                    "cgroup": Path("/proc/self/cgroup").read_text(),
                    "run_id": reservation.run_id, "session_id": reservation.session_id,
                })
                deadline = time.monotonic() + 30
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("fixture release was not observed")
                    time.sleep(0.05)
            finally:
                if child is not None:
                    child.terminate()
                    child.wait(timeout=3)

        return reconcile
