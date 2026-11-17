"""Loopback-only HTTP transport and read-only routing; no workflow loading."""

import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlsplit

import floe

from . import render

HISTORY_SINCE = "7 days ago"
MAX_RECORDS = 1000
CACHE_SECONDS = 3


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"


class Application:
    """Serial request handler with a short, size-bounded page cache."""

    def __init__(self, store, sd, journal):
        self.store, self.sd, self.journal = store, sd, journal
        self.cache = OrderedDict()

    def summaries(self):
        return floe.overview(self.store, self.sd, self.journal,
                             since=HISTORY_SINCE, max_records=MAX_RECORDS)

    def response(self, target):
        try:
            url = urlsplit(target)
            path = unquote(url.path, errors="strict")
        except ValueError:
            return self.error(400, "Invalid request")
        if url.scheme or url.netloc or url.query or url.fragment:
            return self.error(404, "Page not found")
        try:
            if path == "/style.css":
                return Response(200, render.CSS.encode(), "text/css; charset=utf-8")
            cached = self.cache.get(path)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                self.cache.move_to_end(path)
                return cached[1]
            response = self.page(path)
            self.cache[path] = (time.monotonic(), response)
            self.cache.move_to_end(path)
            while len(self.cache) > 16:
                self.cache.popitem(last=False)
            return response
        except floe.NotFoundError as exc:
            return self.error(404, str(exc))
        except floe.ValidationError:
            return self.error(400, "Invalid request")
        except (floe.FloeError, OSError, ValueError) as exc:
            return self.error(503, f"Unable to read workflow state: {exc}")

    def page(self, path):
        if path == "/":
            return Response(200, render.document("Workflows", render.inventory(self.summaries()), path))
        match = re.fullmatch(r"/workflows/([^/]+)(?:/runs/([0-9a-f]{32}))?", path)
        if not match:
            return self.error(404, "Page not found")
        name, run_id = match.groups()
        floe.unit_names(name)  # Validate as a name, including file-like names; never load it.
        if run_id:
            logs = floe.run_logs(name, run_id, self.journal, self.sd, max_records=MAX_RECORDS)
            body = render.run_detail(logs)
        else:
            summary = next((item for item in self.summaries() if item.name == name), None)
            runs = floe.list_runs(name, self.journal, self.sd, since=HISTORY_SINCE,
                                  limit=20, max_records=MAX_RECORDS)
            if summary is None and not runs:
                raise floe.NotFoundError(f"No installed workflow or recent history for {name!r}")
            body = render.workflow(name, summary, runs)
        return Response(200, render.document(name, body, path))

    def error(self, code, message):
        body = f"<h1>{code}</h1><p>{render.text(message)}</p>" + render.link("/", "← Workflows")
        return Response(code, render.document(str(code), body, "/"))


class Server(HTTPServer):
    """Single-threaded by design; idle clients cannot hold a connection forever."""

    def __init__(self, port, app):
        self.app = app
        super().__init__(("127.0.0.1", port), Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = "Floe"
    sys_version = ""

    def do_GET(self):
        # Reject DNS rebinding/foreign Host headers, not just remote bind addresses.
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update(("127.0.0.1", "localhost"))
        received_hosts = self.headers.get_all("Host", [])
        if len(received_hosts) != 1 or received_hosts[0] not in hosts:
            response = self.server.app.error(403, "Only localhost requests are accepted")
        else:
            response = self.server.app.response(self.path)
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(response.body)

def serve(store, sd, journal, *, port=8080):
    """Serve until Ctrl-C. No daemonization, browser launch, or systemd writes."""
    with Server(port, Application(store, sd, journal)) as server:
        print(f"Floe: http://127.0.0.1:{server.server_port}/ (Ctrl-C to stop)", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
