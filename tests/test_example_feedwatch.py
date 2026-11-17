"""examples/feedwatch, offline: feeds are local files and pi is a fake that
records how it was called. No network and no model are involved."""

import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

import floe
from floe_cli.main import main

EXAMPLE = Path(__file__).parents[1] / "examples" / "feedwatch"

FAKE_PI = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
here = Path(__file__).parent
mode = (here / "mode").read_text().strip()
if sys.argv[1:3] == ["auth", "check"]:
    print(json.dumps({{"status": "not_ready" if mode == "not-ready" else "ready"}}))
    sys.exit(0)
batch = json.loads(sys.stdin.read())
with (here / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({{"argv": sys.argv[1:], "batch": batch}}) + "\\n")
if mode == "crash":
    print("model unavailable", file=sys.stderr)
    sys.exit(1)
if mode == "bad":
    reply = [{{"id": item["id"], "decision": "maybe", "reason": "unsure"}} for item in batch]
else:
    reply = [{{"id": item["id"], "decision": ("notify", "digest", "ignore")[n % 3],
              "reason": f"reason {{n}}"}} for n, item in enumerate(batch)]
message = {{"role": "assistant", "provider": "fake", "model": "triage-1", "stopReason": "stop",
           "usage": {{"totalTokens": 42}}, "content": [{{"type": "text", "text": json.dumps(reply)}}]}}
print(json.dumps({{"type": "agent_start"}}))
print(json.dumps({{"type": "message_end", "message": message}}))
'''


def atom(entries) -> str:
    body = "".join(
        f"<entry><id>{id_}</id><title>{title}</title><link href='https://example.org/{id_}'/>"
        f"<updated>{when.isoformat()}</updated><summary>&lt;p&gt;{title} summary&lt;/p&gt;</summary></entry>"
        for id_, title, when in entries)
    return f"<feed xmlns='http://www.w3.org/2005/Atom'><title>t</title>{body}</feed>"


def rss(entries) -> str:
    body = "".join(
        f"<item><guid>{id_}</guid><title>{title}</title><link>https://example.org/{id_}</link>"
        f"<pubDate>{format_datetime(when)}</pubDate><description>{title} text</description></item>"
        for id_, title, when in entries)
    return f"<rss version='2.0'><channel><title>t</title>{body}</channel></rss>"


class Setup:
    def __init__(self, tmp_path):
        self.dir = tmp_path / "feedwatch"
        shutil.copytree(EXAMPLE, self.dir, ignore=shutil.ignore_patterns("config.json", "state", "out"))
        self.pi_dir = tmp_path / "pi"
        self.pi_dir.mkdir()
        (self.pi_dir / "pi").write_text(FAKE_PI)
        (self.pi_dir / "pi").chmod(0o755)
        self.mode("good")
        now = datetime.now(UTC)
        self.feeds = tmp_path / "feeds"
        self.feeds.mkdir()
        (self.feeds / "releases.atom").write_text(atom([
            ("r1", "Release 1.0", now - timedelta(hours=2)),
            ("r2", "Release 1.1", now - timedelta(hours=1)),
            ("r0", "Ancient release", now - timedelta(days=90)),  # older than max_age_days
        ]))
        (self.feeds / "news.rss").write_text(rss([("n1", "Some news", now - timedelta(hours=3))]))
        self.config(feeds=[("releases", "releases.atom"), ("news", "news.rss")])

    def config(self, *, feeds, **overrides):
        cfg = json.loads((EXAMPLE / "config.example.json").read_text())
        cfg.update(pi=str(self.pi_dir / "pi"), node_bin=None,
                   feeds=[{"name": name, "url": (self.feeds / file).as_uri()} for name, file in feeds],
                   **overrides)
        (self.dir / "config.json").write_text(json.dumps(cfg))

    def mode(self, mode):
        (self.pi_dir / "mode").write_text(mode)

    def calls(self):
        path = self.pi_dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def decisions(self):
        path = self.dir / "state" / "decisions.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def run(self, capsys):
        status = main(["run", str(self.dir / "feedwatch.py")])
        out, err = capsys.readouterr()
        events = [json.loads(line) for line in out.splitlines()]  # stdout is only events
        return status, events, err


@pytest.fixture
def fw(tmp_path):
    return Setup(tmp_path)


def failure(events):
    failed = [e for e in events if e["event"] == "task_completed" and e["status"] == "failed"]
    return (failed[0]["task"], failed[0]["error"]) if failed else None


def test_definition_loads_and_example_config_is_complete(fw):
    workflow = floe.load(EXAMPLE / "feedwatch.py")
    assert workflow.name == "feedwatch" and workflow.schedule
    assert workflow.order.index("preflight") < workflow.order.index("triage")


def test_triages_new_items_once_and_publishes(fw, capsys):
    status, events, err = fw.run(capsys)
    assert status == 0, err
    [call] = fw.calls()
    # Newest first; the 90-day-old entry is skipped.
    assert [item["title"] for item in call["batch"]] == ["Release 1.1", "Release 1.0", "Some news"]
    assert [item["id"] for item in call["batch"]] == ["i1", "i2", "i3"]
    decided = fw.decisions()
    assert sorted((r["title"], r["decision"]) for r in decided.values()) == [
        ("Release 1.0", "digest"), ("Release 1.1", "notify"), ("Some news", "ignore")]
    run_id = events[0]["run_id"]
    assert {r["run_id"] for r in decided.values()} == {run_id}
    assert {r["model"] for r in decided.values()} == {"fake/triage-1"}
    inbox = (fw.dir / "out" / "inbox.md").read_text()
    assert "[Release 1.1](https://example.org/r2) (releases): reason 0" in inbox
    [digest] = (fw.dir / "out").glob("digest-*.md")
    assert "[Release 1.0](https://example.org/r1) (releases): reason 1" in digest.read_text()
    assert "Some news" not in inbox + digest.read_text()

    outputs = {path.name: path.read_text() for path in (fw.dir / "out").iterdir()}
    status, events, err = fw.run(capsys)  # nothing new: no agent call, same outputs
    assert status == 0 and len(fw.calls()) == 1 and "nothing new to triage" in err
    assert {path.name: path.read_text() for path in (fw.dir / "out").iterdir()} == outputs


def test_pi_runs_without_tools_or_context(fw, capsys):
    assert fw.run(capsys)[0] == 0
    argv = fw.calls()[0]["argv"]
    for flag in ("-nt", "-ne", "-ns", "-np", "-nc", "--no-mcp", "-na", "--no-session"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "github-copilot/claude-haiku-5.5"
    assert argv[argv.index("--system-prompt") + 1] == str(fw.dir / "system.md")


@pytest.mark.parametrize("mode,error", [
    ("bad", "invalid pi reply: i1: bad decision 'maybe'"),
    ("crash", "pi failed (exit 1): model unavailable"),
])
def test_unusable_agent_reply_saves_nothing_and_next_run_asks_again(fw, capsys, mode, error):
    fw.mode(mode)
    status, events, err = fw.run(capsys)
    assert status == 1
    task, message = failure(events)
    assert task == "triage" and message.startswith(error)
    assert fw.decisions() == {}
    assert not (fw.dir / "out").exists()

    fw.mode("good")
    assert fw.run(capsys)[0] == 0
    assert len(fw.decisions()) == 3
    assert [call["batch"] for call in fw.calls()][-1] == fw.calls()[0]["batch"]


def test_failing_feed_does_not_stop_the_others(fw, capsys):
    fw.config(feeds=[("releases", "releases.atom"), ("gone", "missing.rss")])
    status, events, err = fw.run(capsys)
    assert status == 1
    task, message = failure(events)
    assert task == "report" and message.startswith("feeds failed: gone: URLError")
    assert sorted(r["title"] for r in fw.decisions().values()) == ["Release 1.0", "Release 1.1"]


def test_items_beyond_the_limit_wait_for_the_next_run(fw, capsys):
    fw.config(feeds=[("releases", "releases.atom"), ("news", "news.rss")], max_items_per_run=2)
    assert fw.run(capsys)[0] == 0
    assert fw.run(capsys)[0] == 0
    assert [[item["title"] for item in call["batch"]] for call in fw.calls()] == [
        ["Release 1.1", "Release 1.0"], ["Some news"]]


@pytest.mark.parametrize("problem,error", [
    ("no-config", "config.json not found: copy config.example.json"),
    ("not-ready", "pi is not ready to use"),
    ("no-pi", "pi not found or not executable"),
])
def test_preflight_explains_what_is_missing(fw, capsys, problem, error):
    if problem == "no-config":
        (fw.dir / "config.json").unlink()
    elif problem == "not-ready":
        fw.mode("not-ready")
    else:
        (fw.pi_dir / "pi").unlink()
    status, events, err = fw.run(capsys)
    assert status == 1
    failed = failure(events)
    assert failed[0] == "preflight" and error in failed[1]
    assert fw.calls() == []
