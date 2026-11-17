"""Small HTML documents. All workflow and journal content is escaped as text."""

from datetime import UTC
from html import escape
from urllib.parse import quote

CSS = """
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #222; background: white; }
body { max-width: 64rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
a { color: #195cab; text-underline-offset: .15em; }
header { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 1px solid #ddd; }
header a { font-weight: 600; }
h1 { font-size: 1.5rem; margin-bottom: .5rem; }
h2 { font-size: 1.1rem; margin-top: 2rem; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th, td { padding: .65rem .8rem .65rem 0; border-bottom: 1px solid #e8e8e8; vertical-align: top; }
th { font-weight: 600; }
pre, code { font-family: ui-monospace, monospace; font-size: .9rem; }
pre { overflow-x: auto; padding: 1rem; background: #fafafa; border: 1px solid #e8e8e8; }
.muted, footer { color: #666; }
.succeeded { color: #226b36; } .failed { color: #a32020; }
.running, .incomplete { color: #805500; }
.warning { border-left: 3px solid #a77719; padding-left: .8rem; overflow-wrap: anywhere; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ddd; font-size: .85rem; }
"""


def text(value):
    return escape("—" if value is None else str(value), quote=True)


def link(url, label):
    return f'<a href="{text(url)}">{text(label)}</a>'


def workflow_url(name):
    return "/workflows/" + quote(name, safe="")


def run_url(run):
    return workflow_url(run.workflow) + "/runs/" + quote(run.run_id, safe="")


def date(value):
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def duration(value):
    return f"{value.total_seconds():.1f}s" if value is not None else "—"


def status(value):
    # Only known statuses may become CSS classes.
    style = value if value in ("succeeded", "failed", "running", "incomplete") else "muted"
    return f'<span class="{style}">{text(value.capitalize())}</span>'


def table(headers, rows):
    return ('<div class="scroll"><table><thead><tr>'
            + "".join(f"<th scope=\"col\">{text(h)}</th>" for h in headers)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
            + "</tbody></table></div>")


def warnings(problems):
    return "".join(f'<p class="warning">{text(problem)}</p>' for problem in problems)


def document(title, body, path):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{text(title)} · Floe</title><link rel=\"stylesheet\" href=\"/style.css\">"
            "</head><body><header>" + link("/", "Floe") + link(path, "Refresh")
            + f"</header><main>{body}</main>"
            '<footer>Read-only · All times UTC · Pages cached for up to 3 seconds. '
            "History depends on journal retention.</footer></body></html>").encode("utf-8")


def inventory(summaries):
    rows = []
    for item in summaries:
        latest = (f'<a href="{text(run_url(item.last_run))}">{status(item.last_run.status)}</a>'
                  if item.last_run else "No recent runs")
        if item.last_run and item.last_run.truncated:
            latest += " (partial history)"
        rows.append((link(workflow_url(item.name), item.name) + warnings(item.problems), latest,
                     text(", ".join(item.schedule) or "—"), text(date(item.next_run))))
    return ("<h1>Workflows</h1><p class=\"muted\">Latest runs with journal activity in the last 7 days.</p>"
            + (table(("Workflow", "Latest run", "Schedule", "Next run"), rows)
               if rows else "<p>No installed workflows.</p>"))


def workflow(name, summary, runs):
    body = link("/", "← Workflows") + f"<h1>{text(name)}</h1>"
    if summary:
        body += (f"<p>{text(', '.join(summary.schedule) or 'No schedule available')}"
                 f" · Next run {text(date(summary.next_run))}</p>" + warnings(summary.problems))
    else:
        body += "<p class=\"warning\">This workflow is no longer installed.</p>"
    body += '<h2>Recent runs</h2><p class="muted">Up to 20 runs with journal activity in the last 7 days.</p>'
    rows = [(link(run_url(run), date(run.started) if run.started else run.run_id[:8]),
             status(run.status) + (" (partial history)" if run.truncated else ""),
             text(duration(run.duration))) for run in runs]
    return body + (table(("Started", "Status", "Duration"), rows) if rows else "<p>No recent runs.</p>")


def run_detail(logs):
    run = logs.run
    body = (link(workflow_url(run.workflow), "← " + run.workflow)
            + f"<h1>{text(run.workflow)}</h1><p>{status(run.status)} · {text(date(run.started))}"
            + f" · {text(duration(run.duration))}</p><p>Run: <code>{text(run.run_id)}</code></p>")
    if run.truncated:
        body += warnings(["Partial history: only the newest 1,000 journal records are shown. "
                          "Earlier tasks, output, and start times may be missing; "
                          "an incomplete status is not proof of failure."])
    if run.result:
        body += f"<p>Systemd result: {text(run.result)}</p>"
    body += "<h2>Tasks</h2>"
    rows = [(text(task.name), status(task.status), text(duration(task.duration))) for task in run.tasks]
    body += table(("Task", "Status", "Duration"), rows) if rows else "<p>No task records available.</p>"
    if run.error:
        error = run.error
        if len(error) > 8000:
            error = error[:8000] + " … [error shortened]"
        body += f"<h2>Error</h2><pre>{text(error)}</pre>"
    body += "<h2>Logs</h2><p class=\"muted\">Messages longer than 2,000 characters are shortened.</p><pre>"
    for line in logs.lines:
        message = line.message
        if len(message) > 2000:
            message = message[:2000] + " … [message shortened]"
        body += text(f"{date(line.timestamp)}  {line.task or line.kind}  {message}") + "\n"
    return body + "</pre>"
