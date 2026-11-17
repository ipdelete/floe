"""Read-only pages and real loopback HTTP; never contacts a service manager."""

import http.client
import threading
from html.parser import HTMLParser
from unittest.mock import create_autospec

import pytest
from journal_fakes import FakeJournal, Run

import floe
from floe_cli.main import main
from floe_web.server import Application, Server, serve


@pytest.fixture
def app(tmp_path):
    store = floe.UnitStore(tmp_path / "units")
    script = tmp_path / "workflow.py"
    script.write_text('raise RuntimeError("the web UI must never import me")\n')
    for name in ("mydag", "other.py"):
        for unit, content in floe.Deployment(name, script, ("hourly",)).render_units().items():
            store.write(unit, content)
    sd = create_autospec(floe.Systemd, instance=True)
    sd.show.return_value = {"ActiveState": "inactive", "InvocationID": ""}
    sd.show_all.return_value = [
        ("UnitFileState", "enabled"), ("ActiveState", "active"),
        ("TimersCalendar", "{ OnCalendar=hourly ; next_elapse=@1800000000 }"),
        ("NextElapseUSecRealtime", "@1800000000"),
    ]
    records = Run("a" * 32).succeeded_run().records
    return Application(store, sd, FakeJournal(records))


def html(response):
    assert response.status == 200
    return response.body.decode()


class Links(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])


def test_three_pages_and_links_are_read_only_metadata(app, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("web browsing must not execute workflow code or mutate systemd")

    monkeypatch.setattr(floe, "load", forbidden)
    monkeypatch.setattr(floe, "check_installed", forbidden)
    for name in ("install", "uninstall", "start", "apply", "plan_install"):
        monkeypatch.setattr(floe, name, forbidden)
    home = html(app.response("/"))
    assert "mydag" in home and "other.py" in home and "Succeeded" in home
    assert "2027-01-15 08:00:00" in home  # @1800000000, displayed in UTC
    assert "All times UTC" in home
    assert "/workflows/mydag" in Links(home).links
    assert "/workflows/other.py" in Links(home).links
    workflow = html(app.response("/workflows/other.py"))
    assert "Recent runs" in workflow and "hourly" in workflow
    run_path = "/workflows/other.py/runs/" + "a" * 32
    assert run_path in Links(workflow).links
    detail = html(app.response(run_path))
    assert "Tasks" in detail and "doing fetch" in detail and "doing transform" in detail
    assert "script" not in Links(detail).tags and "form" not in Links(detail).tags
    assert all(call[0] in ("show", "show_all") for call in app.sd.mock_calls)
    assert all(call[1] == "7 days ago" for call in app.journal.calls if call[3] is None)
    assert 1001 in app.journal.limits


def test_logs_errors_and_problems_are_escaped(app):
    r = Run("b" * 32).event("run_started").event("task_started", task='<img src=x onerror="bad()">')
    r.output('<script>alert("bad")</script>')
    r.event("task_completed", task='<img src=x onerror="bad()">', status="failed", error="<b>bad</b>")
    r.event("run_completed", status="failed", error="<b>bad</b>").failed()
    app.journal = FakeJournal(r.records)
    detail = html(app.response("/workflows/mydag/runs/" + "b" * 32))
    assert "&lt;script&gt;" in detail and "&lt;img" in detail and "&lt;b&gt;bad&lt;/b&gt;" in detail
    assert "script" not in Links(detail).tags and "img" not in Links(detail).tags
    assert "Failed" in detail and "exit-code" in detail
    app.sd.show_all.side_effect = floe.SystemdError(["systemctl"], 1, "<bad manager>")
    home = html(app.response("/"))
    assert "&lt;bad manager&gt;" in home and "<bad manager>" not in home


def test_truncated_logs_are_bounded_and_labelled(app):
    r = Run("c" * 32).event("run_started").event("task_started", task="old-task")
    r.output("old output")
    for index in range(1100):
        r.output(f"line {index}: " + "x" * 2100)
    r.event("run_completed", status="succeeded").finished()
    app.journal = FakeJournal(r.records)
    detail = html(app.response("/workflows/mydag/runs/" + "c" * 32))
    assert "Partial history" in detail and "1,000" in detail
    assert "old output" not in detail and "old-task" not in detail
    assert "line 1099" in detail and "message shortened" in detail
    assert "x" * 2001 not in detail
    assert app.journal.read == 1001
    assert app.journal.calls == [("mydag.service", None, True, ("c" * 32,))]


def test_running_and_incomplete_remain_distinct(app):
    r = Run("d" * 32).event("run_started").event("task_started", task="wait")
    app.journal = FakeJournal(r.records)
    app.sd.show.return_value = {"ActiveState": "activating", "InvocationID": r.id}
    assert "Running" in html(app.response("/workflows/mydag/runs/" + r.id))
    app.cache.clear()
    app.sd.show.return_value = {"ActiveState": "inactive", "InvocationID": ""}
    assert "Incomplete" in html(app.response("/workflows/mydag/runs/" + r.id))


def test_errors_are_not_empty_success_and_do_not_hide_other_workflows(app):
    def show(unit, *args, **kwargs):
        if unit == "mydag.timer":
            raise floe.SystemdError(["systemctl"], 1, "manager unavailable")
        return [("ActiveState", "active")]

    app.sd.show_all.side_effect = show
    home = html(app.response("/"))
    assert "manager unavailable" in home and "other.py" in home
    other_row = home.split('href="/workflows/other.py"')[1].split("</tr>")[0]
    assert "Succeeded" in other_row and "manager unavailable" not in other_row
    app.sd.show.side_effect = floe.SystemdError(["systemctl"], 1, "journal state unknown")
    response = app.response("/workflows/mydag/runs/" + "a" * 32)
    assert response.status == 503 and b"journal state unknown" in response.body
    assert b"No recent runs" not in response.body


def test_malformed_backend_data_is_not_reported_as_a_bad_request(app):
    app.journal.records_list[0]["__REALTIME_TIMESTAMP"] = "not-a-timestamp"
    response = app.response("/workflows/mydag/runs/" + "a" * 32)
    assert response.status == 503
    assert b"Unable to read workflow state" in response.body


def test_empty_and_uninstalled_workflows(app):
    app.journal = FakeJournal([])
    assert "No recent runs" in html(app.response("/workflows/mydag"))
    assert app.response("/workflows/missing").status == 404
    app.journal = FakeJournal(Run("a" * 32).succeeded_run().records)
    assert "no longer installed" in html(app.response("/workflows/deleted"))
    empty = Application(floe.UnitStore(app.store.path / "absent"), app.sd, FakeJournal([]))
    assert "No installed workflows" in html(empty.response("/"))


@pytest.mark.parametrize("path,code", [
    ("/missing", 404), ("/workflows/../", 404), ("/workflows/%2Fetc%2Fpasswd", 404),
    ("/workflows/%FF", 400), ("/workflows/%3Cscript%3E", 400),
    ("/workflows/mydag/runs/not-a-run", 404), ("/?action=start", 404),
    ("http://foreign/", 404),
])
def test_invalid_routes(app, path, code):
    assert app.response(path).status == code
    assert not app.journal.calls and not app.sd.mock_calls


def test_short_cache_expires_and_is_size_bounded(app, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("floe_web.server.time.monotonic", lambda: now[0])
    original = html(app.response("/"))
    calls = len(app.journal.calls)
    app.journal.records_list.clear()
    assert html(app.response("/")) == original and len(app.journal.calls) == calls
    now[0] += 4
    assert "No recent runs" in html(app.response("/"))
    assert len(app.journal.calls) > calls
    for index in range(25):
        app.response(f"/unknown-{index}")
    assert len(app.cache) == 16


@pytest.fixture
def http_server(app):
    with Server(0, app) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(timeout=3)
            assert not worker.is_alive()


def request(server, method="GET", path="/", headers=None):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        client.request(method, path, headers=headers or {})
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


def test_http_transport_headers_stylesheet_and_host_check(http_server):
    assert http_server.server_address[0] == "127.0.0.1"
    code, headers, body = request(http_server)
    assert code == 200 and b"Workflows" in body
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert int(headers["Content-Length"]) == len(body)
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    code, headers, css = request(http_server, path="/style.css")
    assert code == 200 and headers["Content-Type"].startswith("text/css")
    assert b"background: white" in css
    assert request(http_server, headers={"Host": "attacker.example"})[0] == 403
    assert request(http_server, "POST")[0] == 501
    assert request(http_server, path="/etc/passwd")[0] == 404


def test_server_ctrl_c_closes_socket_and_prints_only_to_stderr(app, monkeypatch, capsys):
    servers = []

    def stop(server):
        servers.append(server)
        raise KeyboardInterrupt

    monkeypatch.setattr(Server, "serve_forever", stop)
    assert serve(app.store, app.sd, app.journal, port=0) == 0
    out, err = capsys.readouterr()
    assert out == "" and "http://127.0.0.1:" in err and "Ctrl-C" in err
    assert servers[0].fileno() == -1
    assert not app.sd.mock_calls and not app.journal.calls


def test_web_cli_dispatch_and_errors(app, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("floe_web.serve", lambda *args, **kw: calls.append((args, kw)) or 0)
    assert main(["web", "--port", "8123"], store=app.store, sd=app.sd, journal=app.journal) == 0
    assert calls == [((app.store, app.sd, app.journal), {"port": 8123})]
    for port in ("0", "65536", "-1", "abc"):
        with pytest.raises(SystemExit) as exc:
            main(["web", "--port", port])
        assert exc.value.code == 2
    capsys.readouterr()

    def unavailable(*args, **kwargs):
        raise OSError("Address already in use")

    monkeypatch.setattr("floe_web.serve", unavailable)
    assert main(["web"], store=app.store, sd=app.sd, journal=app.journal) == 1
    out, err = capsys.readouterr()
    assert out == "" and "Address already in use" in err
