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
import tempfile
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
            assistant = None
            for line in stream:
                entry = json.loads(line)
                if entry.get("type") == "session_info":
                    name = entry.get("name")
                if entry.get("type") == "message" and entry["message"].get("role") == "assistant":
                    if assistant is not None:
                        return False
                    assistant = entry["message"]
        if name != SESSION_NAME or assistant is None:
            return False
        provider, model = MODEL.split("/", 1)
        content = assistant["content"]
        return (assistant.get("provider") == provider and assistant.get("model") == model
                and assistant.get("stopReason") == "stop" and not assistant.get("errorMessage")
                and isinstance(content, list) and all(block.get("type") == "text" for block in content)
                and "".join(block["text"] for block in content).strip() == REPLY)
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return False


def pi_exit_report(returncode, stderr):
    # Persist only fixed labels, not bytes supplied by Pi, Node, or a provider.
    raw = stderr.lower()
    known = (
        ("permission-denied", (b"eacces", b"permission denied")),
        ("operation-not-permitted", (b"eperm", b"operation not permitted")),
        ("read-only-filesystem", (b"erofs", b"read-only file system")),
        ("missing-file", (b"enoent", b"no such file")),
        ("bad-file-descriptor", (b"ebadf", b"bad file descriptor")),
        ("unsupported-address-family", (b"eafnosupport", b"address family not supported")),
        ("assertion", (b"assertion", b"check failed")),
        ("node-runtime", (b"node.js v", b"fatal error", b"libuv", b"native stack trace")),
        ("stdio", (b"stdout", b"stderr", b"epipe", b"write eio")),
        ("disposal", (b"dispose", b"shutdown")),
        ("size-limit", (b"efbig", b"file too large")),
        ("socket", (b"socket", b"connect")),
        ("worker-thread", (b"worker_thread", b"uv_thread", b"pthread")),
    )
    categories = [name for name, patterns in known if any(pattern in raw for pattern in patterns)]
    return {"returncode": returncode, "stderr": categories or (["unclassified"] if stderr else [])}


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
    # The service limits file size. Raw stderr stays in an unnamed file in
    # private tmpfs; only a bounded, allowlisted diagnosis survives collection.
    with tempfile.TemporaryFile(dir=AGENT) as diagnostics:
        result = subprocess.run(
            ["/node", "/pi/node_modules/.bin/pi", "--print", "--model", MODEL, "--thinking", "off",
             "--session-id", session_id, "--name", SESSION_NAME, "--session-dir", str(SESSIONS),
             "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
             "--no-context-files", PROMPT],
            # Pi's bundled entry point enables Node's compile cache, which
            # flushes at exit and can exceed the sandbox's per-file limit.
            env={**ENV, "PI_CODING_AGENT_DIR": str(AGENT), "PI_OFFLINE": "1", "NODE_DISABLE_COMPILE_CACHE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=diagnostics, timeout=45,
        )
        diagnostics.seek(0)
        report = pi_exit_report(result.returncode, diagnostics.read(32 * 1024))
    write_private_json(OUTPUT, "pi-exit", report)
    if result.returncode != 0:
        raise Blocked("Pi process did not exit successfully")
    if not verify_session(SESSIONS, session_id):
        raise Blocked("native session not verified")


def execute(role, identity, session_id):
    if role not in ("tick", "worker"):
        raise Blocked("unknown smoke role")
    require_isolation(role)
    token = GITHUB.read_text().strip()
    if not token:
        raise Blocked("required GitHub credential unavailable")
    check_github(role, token, identity)
    if role == "worker":
        run_pi(session_id)


FAILURES = frozenset({
    "required isolation unavailable", "required GitHub credential unavailable", "GitHub bad-auth rejection not verified",
    "GitHub identity not verified", "GitHub repository read not verified", "Pi session reservation not fresh",
    "Copilot-only credential required", "Pi process did not exit successfully", "native session not verified",
    "smoke service operation failed",
})


def write_private_json(output, stem, payload):
    temporary = output / f"{stem}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream)
    temporary.replace(output / f"{stem}.json")


def write_outcome(output, run_id, session_id, outcome):
    write_private_json(output, "outcome", {"run_id": run_id, "session_id": session_id, "outcome": outcome})


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
