"""Fixed supervised service payload. Run only inside the smoke sandbox."""

import ctypes
import errno
import json
import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

MODEL = "github-copilot/gpt-6.2-sol"
SESSION_NAME = "29-agentic-dev-loop — supervised Pi smoke"
REPLY = "FLOE-SMOKE-OK"
PROMPT = f"Reply with exactly {REPLY} and nothing else. Do not use any tools."
INVALID = "floe-invalid-smoke-token"
GITHUB = Path("/input/github")
COPILOT = Path("/input/copilot.json")
MODELS = Path("/input/models-store.json")
AGENT = Path("/tmp/agent")
OUTPUT = Path("/output")
SESSIONS = OUTPUT / "sessions"
ENV = {"HOME": "/sandbox-home", "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}


class Blocked(Exception):
    """Fixed diagnostic only; never wrap or echo subprocess output."""


def check_layout(role):
    if (dict(os.environ) != {**ENV, "PWD": "/work"} or any(os.isatty(fd) for fd in (0, 1, 2))
            or any(Path(path).exists() for path in ("/home", "/root", "/run", "/mnt"))
            or (role == "tick" and any(path.exists() for path in (COPILOT, MODELS, Path("/pi"), Path("/node"))))
            or (role == "worker" and not Path("/work/.git").is_dir())):
        raise Blocked("required isolation unavailable")


def require_isolation(role):
    check_layout(role)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
            pass
    except OSError:
        raise Blocked("required isolation unavailable") from None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            pass
    except OSError as exc:
        if exc.errno not in (errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES):
            raise Blocked("required isolation unavailable") from None
    else:
        raise Blocked("required isolation unavailable")
    try:
        keyutils = ctypes.CDLL("libkeyutils.so.1", use_errno=True)
        # KEY_SPEC_SESSION_KEYRING; no creation or read of key material.
        forbidden = keyutils.keyctl_get_keyring_ID(-3, 0) == -1 and ctypes.get_errno() in (errno.EPERM, errno.EACCES)
    except (OSError, AttributeError):
        forbidden = False
    if not forbidden:
        raise Blocked("required isolation unavailable")


def github(endpoint, token):
    selector = ".login" if endpoint == "user" else ".number"
    try:
        result = subprocess.run(
            ["/usr/bin/gh", "api", "--hostname", "github.com", "--method", "GET", "--include",
             "--jq", selector, endpoint],
            env={**ENV, "GH_TOKEN": token, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
        )
        headers, _, body = result.stdout.partition(b"\r\n\r\n")
        if not body:
            headers, _, body = result.stdout.partition(b"\n\n")
        match = re.match(rb"HTTP/\S+ (\d{3})\b", headers)
        status = int(match[1]) if match else None
        return status, body.decode().strip() if result.returncode == 0 else ""
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None, ""


def check_github(role, token, identity):
    if github("user", INVALID)[0] != 401:
        raise Blocked("GitHub bad-auth rejection not verified")
    if github("user", token) != (200, identity):
        raise Blocked("GitHub identity not verified")
    if role == "tick" and github("repos/ipdelete/floe/issues/29", token) != (200, "29"):
        raise Blocked("GitHub repository read not verified")


def private(path, kind, permissions):
    info = path.lstat()
    return kind(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == permissions


def verify_session(directory, session_id):
    try:
        if str(UUID(session_id)) != session_id or not private(directory, stat.S_ISDIR, 0o700):
            return False
        [path] = directory.iterdir()
        if (not path.name.endswith(f"_{session_id}.jsonl") or not private(path, stat.S_ISREG, 0o600)
                or path.stat().st_nlink != 1 or path.stat().st_size > 256 * 1024):
            return False
        with path.open() as stream:
            header = json.loads(stream.readline())
            if header.get("type") != "session" or header.get("id") != session_id or header.get("cwd") != "/work":
                return False
            name = None
            for line in stream:
                entry = json.loads(line)
                if entry.get("type") == "session_info":
                    name = entry.get("name")
        return name == SESSION_NAME
    except (OSError, ValueError, AttributeError):
        return False


def run_pi(session_id):
    if str(UUID(session_id)) != session_id or any(SESSIONS.iterdir()):
        raise Blocked("Pi session reservation not fresh")
    auth = json.loads(COPILOT.read_text())
    if not isinstance(auth, dict) or set(auth) != {"github-copilot"}:
        raise Blocked("Copilot-only credential required")
    AGENT.mkdir(mode=0o700)
    (AGENT / "auth.json").write_text(json.dumps(auth))
    (AGENT / "settings.json").write_text(json.dumps({
        "retry": {"enabled": False, "provider": {"maxRetries": 0}},
    }))
    if MODELS.exists():
        (AGENT / "models-store.json").write_bytes(MODELS.read_bytes())
    result = subprocess.run(
        ["/node", "/pi/node_modules/.bin/pi", "--print", "--model", MODEL, "--thinking", "off",
         "--session-id", session_id, "--name", SESSION_NAME, "--session-dir", str(SESSIONS),
         "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
         "--no-context-files", PROMPT],
        env={**ENV, "PI_CODING_AGENT_DIR": str(AGENT), "PI_OFFLINE": "1"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45,
    )
    if result.returncode != 0 or result.stdout.strip() != REPLY.encode():
        raise Blocked("Pi smoke response not verified")
    if not verify_session(SESSIONS, session_id):
        raise Blocked("native session not verified")


def execute(role, identity, session_id):
    if role not in ("tick", "worker"):
        raise Blocked("unknown smoke role")
    require_isolation(role)
    token = GITHUB.read_text().strip()
    if not token or (role == "tick" and not token.startswith("github_pat_")):
        raise Blocked("required GitHub credential unavailable")
    check_github(role, token, identity)
    if role == "worker":
        run_pi(session_id)


FAILURES = frozenset({
    "required isolation unavailable", "required GitHub credential unavailable", "GitHub bad-auth rejection not verified",
    "GitHub identity not verified", "GitHub repository read not verified", "Pi session reservation not fresh",
    "Copilot-only credential required", "Pi smoke response not verified", "native session not verified",
    "smoke service operation failed",
})


def write_outcome(output, run_id, session_id, outcome):
    temporary = output / "outcome.tmp"
    temporary.write_text(json.dumps({"run_id": run_id, "session_id": session_id, "outcome": outcome}))
    temporary.replace(output / "outcome.json")


def main(role, identity, run_id, session_id):
    os.umask(0o077)
    try:
        execute(role, identity, session_id)
    except Blocked as exc:
        outcome = str(exc) if str(exc) in FAILURES else "smoke service operation failed"
    except (OSError, ValueError, subprocess.SubprocessError):
        outcome = "smoke service operation failed"
    else:
        outcome = "ok"
    write_outcome(OUTPUT, run_id, session_id, outcome)
    return 0 if outcome == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
