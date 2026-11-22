"""Fast-forward the local pr-agent checkout, verify it, then publish to the fork."""

import os
import signal
import subprocess
import sys
from pathlib import Path

from floe import RetryPolicy

WORKFLOW = "pr-agent-maintenance"
SCHEDULE = ["*-*-* 19:00:00 America/New_York"]

REPO = Path.home() / "src/pr-agent"
GIT = Path("/usr/bin/git")
UV = Path.home() / ".local/bin/uv"
DOCTOR = Path.home() / ".local/bin/pr-agent"
GH = Path("/usr/bin/gh")


class TransientFailure(RuntimeError):
    """A network command can safely be tried once more."""


def log(message):
    print(message, file=sys.stderr, flush=True)


def command(tool, *args, timeout=120, network=False, doctor=False):
    env = dict(os.environ)
    # The doctor wrapper invokes uv and gh by name; systemd does not read shell profiles.
    env["PATH"] = os.pathsep.join((str(UV.parent), str(GH.parent), "/usr/bin", "/bin",
                                  env.get("PATH", "")))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    argv = [str(tool), *args]
    log("$ " + " ".join(argv))
    try:
        with subprocess.Popen(argv, cwd=REPO, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill helpers too, before a retry can start another command.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The process group has already exited.
                process.communicate()
                raise
            result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except FileNotFoundError:
        raise RuntimeError(f"Missing executable or checkout: {tool}, {REPO}") from None
    except subprocess.TimeoutExpired:
        error = TransientFailure if network or doctor else RuntimeError
        raise error(f"{tool.name} timed out after {timeout}s") from None
    for output in (result.stdout, result.stderr):
        if output:
            log(output.rstrip())
    if result.returncode:
        detail = (result.stderr + result.stdout).lower()
        transient = any(text in detail for text in (
            "could not resolve host", "could not resolve hostname", "connection timed out",
            "connection reset", "failed to connect", "connection refused",
            "remote end hung up", "http 502", "http 503", "http 504",
            "requested url returned error: 502", "requested url returned error: 503",
            "requested url returned error: 504",
        ))
        error = TransientFailure if doctor or (network and transient) else RuntimeError
        raise error(f"{tool.name} {' '.join(args)} failed (exit {result.returncode})")
    return result.stdout.strip()


def clean_main():
    if command(GIT, "symbolic-ref", "--short", "HEAD") != "main":
        raise RuntimeError("pr-agent must have main checked out; no automatic branch switching")
    if command(GIT, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("pr-agent working tree is dirty; no automatic stash or cleanup")


def preflight():
    if not REPO.is_dir():
        raise RuntimeError(f"Missing pr-agent checkout: {REPO}")
    for tool in (GIT, UV, DOCTOR, GH):
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise RuntimeError(f"Missing executable: {tool}")
    clean_main()


def fetch():
    command(GIT, "fetch", "upstream", network=True)
    command(GIT, "fetch", "origin", network=True)


def inspect():
    clean_main()
    local = command(GIT, "rev-parse", "main")
    origin = command(GIT, "rev-parse", "origin/main")
    upstream = command(GIT, "rev-parse", "upstream/main")
    # Also permits recovery after verification failed: origin <= local <= upstream.
    command(GIT, "merge-base", "--is-ancestor", origin, local)
    command(GIT, "merge-base", "--is-ancestor", local, upstream)
    count = command(GIT, "rev-list", "--count", f"{local}..{upstream}")
    log(f"Local {local}; fork {origin}; target {upstream}; incoming commits: {count}")
    return {"old": local, "target": upstream}


def fast_forward(*, revision):
    clean_main()
    if command(GIT, "rev-parse", "HEAD") != revision["old"]:
        raise RuntimeError("Checkout changed since inspection; refusing to update")
    command(GIT, "merge", "--ff-only", revision["target"])


def sync():
    command(UV, "sync", timeout=600)


def doctor():
    command(DOCTOR, "doctor", timeout=180, doctor=True)


def push(*, revision):
    clean_main()
    if command(GIT, "rev-parse", "HEAD") != revision["target"]:
        raise RuntimeError("Checkout changed since update; refusing to push")
    command(GIT, "push", "origin", "main:main", network=True)


def verify(*, revision):
    clean_main()
    local = command(GIT, "rev-parse", "HEAD")
    remote = command(GIT, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    if local != revision["target"] or remote.split()[0] != local:
        raise RuntimeError("Local main and fork do not match the verified upstream revision")
    log(f"Verified and published {local}; working tree is clean")


tasks = {"preflight": preflight, "fetch": fetch, "inspect": inspect,
         "fast-forward": fast_forward, "sync": sync, "doctor": doctor,
         "push": push, "verify": verify}
_steps = list(tasks)
dag = {name: {_steps[i - 1]} if i else set() for i, name in enumerate(_steps)}
inputs = {name: {"revision": "inspect"} for name in ("fast-forward", "push", "verify")}
retries = {name: RetryPolicy(max_attempts=2, delay_seconds=60,
                             retry_on=(TransientFailure,))
           for name in ("fetch", "doctor", "push")}
