"""feedwatch: fetch feeds, let an agent (pi) triage new items, publish its decisions.

Requires pi (https://pi.dev) with a model login; see README.md in this directory.
Floe itself needs only the standard library, and so does this workflow, but pi
needs Node.js.

- The agent gets no tools and only the item text; feed content is untrusted,
  and so is the agent's reply, which must hold exactly one valid decision per
  item or the run fails without saving any.
- Decisions are saved before anything is published, so a failed run never asks
  the agent twice about an item; the next scheduled run continues from there.
- Outputs are rebuilt from the saved decisions on every run and replaced
  atomically, so repeating a run cannot duplicate them.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feeds

WORKFLOW = "feedwatch"
SCHEDULE = ["*-*-* 08,13,18:00:00"]

BASE = Path(__file__).resolve().parent
STATE = BASE / "state"
OUT = BASE / "out"
DECISIONS = ("notify", "digest", "ignore")
CONFIG_KEYS = ("pi", "node_bin", "model", "thinking", "pi_timeout_seconds",
               "max_items_per_run", "max_age_days", "feeds")


# --- helpers ---------------------------------------------------------------------

def log(message: str) -> None:
    print(message, file=sys.stderr)


def run_id() -> str:
    """This run's ID, set by Floe while it runs; the ID that `floe runs` shows."""
    return os.environ["FLOE_RUN_ID"]


def config() -> dict:
    path = BASE / "config.json"
    try:
        cfg = json.loads(path.read_text())
    except FileNotFoundError:
        raise RuntimeError(f"{path} not found: copy config.example.json to config.json and edit it") from None
    missing = [key for key in CONFIG_KEYS if key not in cfg]
    if missing:
        raise RuntimeError(f"{path} is missing: {', '.join(missing)}")
    return cfg


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_text(path: Path, text: str) -> None:
    """Replace the file atomically: readers see the old or the new content, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_json(path: Path, data) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def this_run(name: str) -> dict:
    """A state file written earlier in this run, never a stale one from another run."""
    data = read_json(STATE / name, {})
    if data.get("run_id") != run_id():
        raise RuntimeError(f"state/{name} was not written by this run")
    return data


def decisions() -> dict[str, dict]:
    return read_json(STATE / "decisions.json", {})


def pi_command(cfg: dict) -> tuple[str, dict]:
    """pi's path and environment. Services do not get your shell's PATH, so a
    node installed by a version manager has to be added explicitly."""
    env = dict(os.environ)
    if cfg["node_bin"]:
        env["PATH"] = f"{Path(cfg['node_bin']).expanduser()}{os.pathsep}{env.get('PATH', '')}"
    return str(Path(cfg["pi"]).expanduser()), env


# --- tasks -------------------------------------------------------------------------

def preflight():
    cfg = config()
    pi, env = pi_command(cfg)
    if not os.access(pi, os.X_OK):
        raise RuntimeError(f"pi not found or not executable: {pi}")
    try:
        result = subprocess.run([pi, "auth", "check", "--model", cfg["model"], "--json"],
                                env=env, capture_output=True, text=True, timeout=60)
        status = json.loads(result.stdout)["status"]
    except (ValueError, KeyError, TypeError):
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"pi auth check failed (exit {result.returncode}): {detail}") from None
    if status != "ready":
        raise RuntimeError(f"pi is not ready to use {cfg['model']} ({status}): log in with pi and /login")
    log(f"pi ready: {cfg['model']}")


def fetch():
    cfg = config()
    items, errors = [], []
    for source in cfg["feeds"]:
        try:
            found = feeds.fetch(source["name"], source["url"])
        except Exception as exc:  # one broken feed must not hide the others
            errors.append({"feed": source["name"], "error": f"{type(exc).__name__}: {exc}"})
            log(f"  ✗ {source['name']}: {type(exc).__name__}: {exc}")
            continue
        items.extend(item.to_dict() for item in found)
        log(f"  ✓ {source['name']}: {len(found)} items")
    write_json(STATE / "fetched.json", {"run_id": run_id(), "items": items, "errors": errors})
    if errors and not items:
        raise RuntimeError(f"all {len(errors)} feeds failed")


def select():
    cfg = config()
    fetched = this_run("fetched.json")
    decided = decisions()
    cutoff = datetime.now(UTC) - timedelta(days=cfg["max_age_days"])
    new, keys, too_old = [], set(), 0
    for item in fetched["items"]:
        if item["key"] in decided or item["key"] in keys:
            continue
        keys.add(item["key"])
        if item["published"] and datetime.fromisoformat(item["published"]) < cutoff:
            too_old += 1
            continue
        new.append(item)
    new.sort(key=lambda item: item["published"] or "", reverse=True)
    pending = new[: cfg["max_items_per_run"]]
    write_json(STATE / "pending.json", {"run_id": run_id(), "items": pending})
    log(f"{len(new)} new items: {len(pending)} to triage, {len(new) - len(pending)} left for later runs; "
        f"{too_old} older than {cfg['max_age_days']} days skipped")


def ask_pi(cfg: dict, batch: list[dict]) -> tuple[object, dict]:
    """One pi call without tools, extensions, skills, context files, or a saved
    session. Returns the parsed reply and the model's metadata."""
    pi, env = pi_command(cfg)
    command = [
        pi, "--mode", "json",
        "-nt", "-ne", "-ns", "-np", "-nc", "--no-mcp", "-na", "--no-session", "--no-themes", "--offline",
        "--model", cfg["model"], "--thinking", cfg["thinking"],
        "--system-prompt", str(BASE / "system.md"),
        f"@{BASE / 'interests.md'}",
        "Triage the feed items given above (JSON array). Follow the output format exactly.",
    ]
    STATE.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, input=json.dumps(batch, ensure_ascii=False), env=env, cwd=STATE,
                            capture_output=True, text=True, timeout=cfg["pi_timeout_seconds"])
    final = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (isinstance(event, dict) and event.get("type") == "message_end"
                and isinstance(event.get("message"), dict) and event["message"].get("role") == "assistant"):
            final = event["message"]
    if result.returncode != 0 or final is None:
        raise RuntimeError(f"pi failed (exit {result.returncode}): {result.stderr.strip()[-500:]}")
    text = "".join(part.get("text", "") for part in final.get("content", [])
                   if isinstance(part, dict) and part.get("type") == "text")
    write_text(STATE / "last_reply.txt", text)
    meta = {"model": f"{final.get('provider')}/{final.get('model')}",
            "stop_reason": final.get("stopReason"),
            "tokens": (final.get("usage") or {}).get("totalTokens")}
    if meta["stop_reason"] != "stop":
        raise RuntimeError(f"pi reply ended with {meta['stop_reason']!r}: {final.get('errorMessage', '')}")
    try:
        return json.loads(text), meta
    except ValueError as exc:
        raise RuntimeError(f"pi reply is not JSON ({exc}); see state/last_reply.txt") from None


def validate(reply, batch: list[dict]) -> list[dict]:
    """The reply is untrusted: exactly one valid decision per item, in order."""
    if not isinstance(reply, list) or not all(isinstance(entry, dict) for entry in reply):
        raise RuntimeError("pi reply is not a JSON array of objects")
    expected = [item["id"] for item in batch]
    got = [entry.get("id") for entry in reply]
    if got != expected:
        raise RuntimeError(f"pi reply ids {got} do not match {expected}")
    problems = []
    for entry in reply:
        if entry.get("decision") not in DECISIONS:
            problems.append(f"{entry['id']}: bad decision {entry.get('decision')!r}")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            problems.append(f"{entry['id']}: missing reason")
    if problems:
        raise RuntimeError("invalid pi reply: " + "; ".join(problems))
    return reply


def triage():
    cfg = config()
    items = this_run("pending.json")["items"]
    if not items:
        log("nothing new to triage")
        return
    # Short ids keep the prompt small and the reply easy to check.
    batch = [{"id": f"i{n}", "feed": item["feed"], "title": item["title"], "summary": item["summary"]}
             for n, item in enumerate(items, 1)]
    reply, meta = ask_pi(cfg, batch)
    reply = validate(reply, batch)
    decided = decisions()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for item, entry in zip(items, reply, strict=True):
        reason = entry["reason"].strip()
        decided[item["key"]] = {**item, "decision": entry["decision"], "reason": reason,
                                "model": meta["model"], "decided_at": now, "run_id": run_id()}
        log(f"  {entry['decision']:6}  {item['title'][:70]}  ({reason})")
    write_json(STATE / "decisions.json", decided)
    log(f"{meta['model']} decided {len(reply)} items using {meta['tokens']} tokens")


def publish():
    """Rebuild every output from the saved decisions: safe to repeat."""
    decided = sorted(decisions().values(), key=lambda record: (record["decided_at"], record["key"]))

    def line(record: dict) -> str:
        return f"- [{record['title']}]({record['link']}) ({record['feed']}): {record['reason']}\n"

    inbox = [f"{r['decided_at'][:16]} {line(r)}" for r in decided if r["decision"] == "notify"]
    write_text(OUT / "inbox.md", "# feedwatch inbox\n\n" + "".join(reversed(inbox)))
    weeks: dict[str, list[str]] = {}
    for record in decided:
        if record["decision"] == "digest":
            week = datetime.fromisoformat(record["decided_at"]).strftime("%G-W%V")
            weeks.setdefault(week, []).append(line(record))
    for week, lines in weeks.items():
        write_text(OUT / f"digest-{week}.md", f"# feedwatch digest {week}\n\n" + "".join(lines))
    log(f"published {len(inbox)} inbox items and {sum(map(len, weeks.values()))} digest items")


def report():
    fetched = this_run("fetched.json")
    mine = [r for r in decisions().values() if r["run_id"] == run_id()]
    counts = ", ".join(f"{sum(r['decision'] == name for r in mine)} {name}" for name in DECISIONS)
    log(f"fetched {len(fetched['items'])} items; decided {len(mine)}: {counts}")
    if fetched["errors"]:
        raise RuntimeError("feeds failed: " + "; ".join(f"{e['feed']}: {e['error']}" for e in fetched["errors"]))


tasks = {
    "preflight": preflight,
    "fetch": fetch,
    "select": select,
    "triage": triage,
    "publish": publish,
    "report": report,
}

dag = {
    "preflight": set(),
    "fetch": set(),
    "select": {"fetch"},
    "triage": {"preflight", "select"},
    "publish": {"triage"},
    "report": {"publish"},
}
