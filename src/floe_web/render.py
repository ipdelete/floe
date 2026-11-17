"""Small HTML documents. All workflow and journal content is escaped as text."""

from datetime import datetime, timedelta
from html import escape
from shlex import quote as shell_arg
from urllib.parse import quote

from .assets import icon_svg

CSS = """
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #222; background: white; }
body { max-width: 64rem; margin: 1.5rem auto; padding: 0 1rem; line-height: 1.5; }
a { color: #195cab; text-underline-offset: .15em; }
.lucide { display: inline-block; width: 1rem; height: 1rem; margin-inline-end: .35em; vertical-align: -.125em; flex-shrink: 0; }
main { overflow-wrap: anywhere; }
.skip-link { position: absolute; top: -5rem; }
.skip-link:focus { top: .5rem; z-index: 1; background: white; padding: .5rem; border: 1px solid #195cab; }
:target { scroll-margin-top: 1rem; }
.log-group:target { outline: 1px solid #195cab; }
header { display: flex; justify-content: space-between; align-items: center; gap: 1rem; border-bottom: 1px solid #ddd; }
header a { display: inline-flex; align-items: center; min-height: 44px; font-weight: 600; }
.refresh { display: flex; align-items: center; justify-content: end; gap: 1rem; flex-wrap: wrap; }
.updated { color: #666; font-size: .8rem; }
main > nav { margin-top: 1rem; }
h1 { font-size: 1.5rem; margin-bottom: .5rem; }
h2 { font-size: 1.1rem; margin-top: 2rem; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th, td { padding: .65rem .8rem .65rem 0; border-bottom: 1px solid #e8e8e8; vertical-align: top; }
th { font-weight: 600; overflow-wrap: normal; }
.cell { min-width: 0; overflow-wrap: anywhere; }
pre, code, .logs { font-family: ui-monospace, monospace; font-size: .9rem; }
pre { padding: 1rem; background: #fafafa; border: 1px solid #e8e8e8; white-space: pre-wrap; overflow-wrap: anywhere; }
code { overflow-wrap: anywhere; }
.muted, footer, .systemd { color: #666; }
.run-meta, .timer-state { font-size: .85rem; margin-top: .2rem; }
.log-group { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .5rem; padding: .7rem .8rem .3rem; border-top: 1px solid #e8e8e8; font-family: system-ui, sans-serif; }
.log-group strong { min-width: 0; overflow-wrap: anywhere; }
.log-group a { font-size: .8rem; }
.succeeded { color: #226b36; } .failed { color: #a32020; }
.running, .incomplete { color: #805500; }
.cell span.succeeded, .cell span.failed, .cell span.running, .cell span.incomplete { white-space: nowrap; }
.warning { border-left: 3px solid #a77719; padding-left: .8rem; overflow-wrap: anywhere; }
.logs { list-style: none; padding: .5rem 0; background: #fafafa; border: 1px solid #e8e8e8; }
.log-line { display: grid; grid-template-columns: 5rem 7rem minmax(0, 1fr); gap: .25rem .75rem; padding: .3rem .8rem; }
.log-time { color: #666; }
.log-task, .log-message { min-width: 0; overflow-wrap: anywhere; }
.log-message { white-space: pre-wrap; }
.log-date { padding: .5rem .8rem; color: #666; font-family: system-ui, sans-serif; font-size: .8rem; }
summary { cursor: pointer; padding: .5rem 0; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ddd; font-size: .85rem; }
@media (max-width: 640px) {
  .updated { max-width: 10rem; text-align: right; }
  .refresh { gap: .5rem; }
  .inventory, .inventory tbody, .inventory tr { display: block; }
  .inventory thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  .inventory tr { padding: .7rem 0; border-bottom: 1px solid #ddd; }
  .inventory td { display: flex; flex-wrap: wrap; gap: .2rem .5rem; padding: .3rem 0; border: 0; }
  .inventory td::before { content: attr(data-label); color: #666; flex: 0 0 6rem; }
  .inventory .cell { flex: 1 1 10rem; }
  .inventory td:first-child { display: block; }
  .inventory td:first-child::before { content: none; }
  .inventory td:first-child > .cell > a { font-weight: 600; }
  .log-line { grid-template-columns: 4.5rem minmax(0, 1fr); gap: .1rem .5rem; }
  .log-message { grid-column: 2; }
}
"""


def text(value):
    return escape("—" if value is None else str(value), quote=True)


def icon(name):
    source = icon_svg(name)
    # Keep upstream files intact; omit only the leading license comment in markup.
    svg = source[source.index("<svg"):].strip()
    return svg.replace("<svg", '<svg aria-hidden="true" focusable="false"', 1)


def link(url, label, *, icon_name=None):
    contents = (icon(icon_name) if icon_name else "") + text(label)
    return f'<a href="{text(url)}">{contents}</a>'


def workflow_url(name):
    return "/workflows/" + quote(name, safe="")


def run_url(run):
    return workflow_url(run.workflow) + "/runs/" + quote(run.run_id, safe="")


def timezone_label(value=None):
    local = (value or datetime.now().astimezone()).astimezone()
    offset = local.strftime("%z")
    return f"{local.tzname()} (UTC{offset[:3]}:{offset[3:]})"


def time_element(value, label):
    local = value.astimezone()
    full = f"{local:%Y-%m-%d %H:%M:%S} {timezone_label(local)}"
    return f'<time datetime="{text(value.isoformat())}" title="{text(full)}">{text(label)}</time>'


def timestamp(value, *, brief=False, now=None):
    if value is None:
        return "—"
    local = value.astimezone()
    label = local.strftime("%Y-%m-%d %H:%M:%S %Z")
    if brief:
        today = (now or datetime.now().astimezone()).astimezone().date()
        if local.date() == today:
            label = f"Today {local:%H:%M}"
        elif local.date() == today + timedelta(days=1):
            label = f"Tomorrow {local:%H:%M}"
        else:
            label = f"{local:%Y-%m-%d %H:%M}"
    return time_element(value, label)


def duration(value):
    if value is None:
        return "—"
    seconds = value.total_seconds()
    return "<0.1s" if 0 < seconds < .1 else f"{seconds:.1f}s"


def status(value, *, label=None):
    # Only known statuses may become CSS classes.
    style = value if value in ("succeeded", "failed", "running", "incomplete") else "muted"
    name = {"succeeded": "check", "failed": "x", "running": "clock-3", "incomplete": "circle-dashed"}.get(value)
    contents = (icon(name) if name else "") + text(label if label is not None else value.capitalize())
    return f'<span class="{style}">{contents}</span>'


def table(headers, rows, *, css_class=""):
    return (f'<div class="scroll"><table class="{text(css_class)}" role="table"><thead><tr>'
            + "".join(f'<th scope="col">{text(h)}</th>' for h in headers)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f'<td data-label="{text(header)}"><div class="cell">{cell}</div></td>'
                                      for header, cell in zip(headers, row, strict=True)) + "</tr>" for row in rows)
            + "</tbody></table></div>")


def warnings(problems):
    return "".join(f'<p class="warning">{text(problem)}</p>' for problem in problems)


def back(url, label):
    return '<nav aria-label="Back">' + link(url, label, icon_name="arrow-left") + "</nav>"


def timer_status(summary):
    state = summary.timer_state
    if state is None:
        return '<div class="timer-state muted">Timer state unavailable</div>'
    active = state["ActiveState"] or "activity unavailable"
    enabled = state["UnitFileState"] or "enablement unavailable"
    style = "muted" if active == "active" and enabled == "enabled" else "incomplete"
    return f'<div class="timer-state {style}">Timer: {text(active)} · {text(enabled)}</div>'


def inspection_help(name=None, run_id=None):
    if name is None:
        commands = ["floe list"]
    else:
        target = "--name " + shell_arg(name)
        commands = [f"floe status {target}", f"floe runs {target}",
                    f"floe logs {target}" + (" " + shell_arg(run_id) if run_id else "")]
    return "<pre>" + text("\n".join(commands)) + "</pre>"


def no_recent_runs(name):
    return ('<p>No recent runs.</p><p>No retained journal activity was found in the last 7 days. '
            'Direct <code>floe run</code> executions are not stored here.</p>'
            '<p>Check status or look further back from a terminal:</p><pre>'
            + text(f'floe status --name {shell_arg(name)}\nfloe runs --name {shell_arg(name)} --since "30 days ago"')
            + "</pre>")


def document(title, body, path):
    now = datetime.now().astimezone()
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{text(title)} · Floe</title><link rel=\"stylesheet\" href=\"/style.css\">"
            '</head><body><a class="skip-link" href="#content">Skip to content</a><header>' + link("/", "Floe")
            + '<div class="refresh"><span class="updated">Updated '
            + time_element(now, f"{now:%H:%M:%S %Z}") + "</span>" + link(path, "Refresh", icon_name="refresh-cw") + "</div>"
            + f'</header><main id="content" tabindex="-1">{body}</main>'
            f'<footer>Read-only · Times: {text(timezone_label(now))} (server local).'
            '<details><summary>History and display limits</summary>'
            "<p>Inventory and workflow pages discover journal activity in the last 7 days, "
            "with up to 20 runs on a workflow page. Each selected run shows its newest 1,000 "
            "journal records; partial history is marked. Messages over 2,000 characters and "
            "errors over 8,000 characters are shortened. Pages are cached for up to 3 seconds. "
            "History depends on journal retention. Schedules use local time unless they specify a timezone. "
            "Timer activity and enablement are separate from run results; unavailable state is not disabled. "
            "Use the CLI for unabridged logs.</p>"
            "</details></footer></body></html>").encode("utf-8")


def inventory(summaries):
    rows = []
    for item in summaries:
        latest = "No recent runs"
        if item.last_run:
            run = item.last_run
            latest = (f'<a href="{text(run_url(run))}">{status(run.status)}</a>'
                      f'<div class="muted run-meta">{timestamp(run.started, brief=True)}'
                      f" · {text(duration(run.duration))}</div>")
            if run.truncated:
                latest += " (partial history)"
        problems = warnings(item.problems)
        if item.problems:
            problems += ('<details><summary>Inspect in terminal</summary>'
                         + inspection_help(item.name) + '</details>')
        rows.append((link(workflow_url(item.name), item.name) + problems, latest,
                     f'<code>{text(", ".join(item.schedule) or "—")}</code>' + timer_status(item),
                     timestamp(item.next_run, brief=True)))
    return ("<h1>Workflows</h1>"
            + (table(("Workflow", "Latest run", "Schedule", "Next run"), rows, css_class="inventory")
               if rows else '<p>No installed workflows.</p><p>Install a trusted workflow file from a terminal:</p>'
               '<pre>floe install /path/to/workflow.py</pre><p>Installation enables its timer.</p>'))


def workflow(name, summary, runs):
    body = back("/", "Workflows") + f"<h1>{text(name)}</h1>"
    if summary:
        body += (f'<p>Schedule: <code>{text(", ".join(summary.schedule) or "Not available")}</code>'
                 f"<br>Next run: {timestamp(summary.next_run, brief=True)}</p>" + timer_status(summary)
                 + warnings(summary.problems))
        if summary.problems:
            body += ('<details><summary>Inspect in terminal</summary>' + inspection_help(name) + '</details>')
    else:
        body += '<p class="warning">This workflow is no longer installed.</p>'
    body += "<h2>Recent runs</h2>"
    rows = [(f'<a href="{text(run_url(run))}">{timestamp(run.started) if run.started else text(run.run_id[:8])}</a>',
             status(run.status) + (" (partial history)" if run.truncated else ""),
             text(duration(run.duration))) for run in runs]
    return body + (table(("Started", "Status", "Duration"), rows) if rows else no_recent_runs(name))


def shorten(message, limit=2000, label="message"):
    return message if len(message) <= limit else message[:limit] + f" … [{label} shortened]"


def log_message(line):
    event = line.event
    if event is None:
        return text(shorten(line.message))
    if event.event == "run_started":
        message = "Run started"
    elif event.event == "task_started":
        message = "Started"
    else:
        message = ("Run " + event.status if event.event == "run_completed"
                   else event.status.capitalize())
    if event.error:
        message += ": " + event.error
    return text(shorten(message))


def task_log_targets(logs):
    targets = {}
    for index, line in enumerate(logs.lines):
        task = line.event.task if line.event else line.task
        if task is not None:
            targets.setdefault(task, f"log-{index}")
    return targets


def log_view(logs):
    body = '<ol class="logs" aria-label="Run logs">'
    day = None
    previous_task = None
    for index, line in enumerate(logs.lines):
        local = line.timestamp.astimezone()
        date_label = f"{local:%Y-%m-%d} {timezone_label(local)}"
        if date_label != day:
            body += f'<li class="log-date">{text(date_label)}</li>'
            day = date_label
        task = line.event.task if line.event else line.task
        if task is not None and task != previous_task:
            body += (f'<li class="log-group" id="log-{index}" tabindex="-1"><strong>{text(task)}</strong>'
                     + link("#tasks", "Back to tasks") + "</li>")
        previous_task = task
        kind = "systemd" if line.kind == "systemd" else ""
        label = task or ("run" if line.event else line.kind)
        body += (f'<li class="log-line {kind}"><span class="log-time">'
                 + time_element(line.timestamp, f"{local:%H:%M:%S}") + "</span>"
                 + f'<span class="log-task">{text(label)}</span>'
                 + f'<div class="log-message">{log_message(line)}</div></li>')
    body += "</ol>" if logs.lines else "</ol><p>No log records available.</p>"
    raw = [line for line in logs.lines if line.event is not None]
    if raw:
        body += '<details class="raw-events"><summary>Raw lifecycle events</summary><pre>'
        body += "\n".join(text(shorten(line.message)) for line in raw)
        body += "</pre></details>"
    return body


def run_detail(logs):
    run = logs.run
    body = (back(workflow_url(run.workflow), run.workflow)
            + f'<h1>{status(run.status, label="Run " + run.status)}</h1><p>{timestamp(run.started)}'
            + f' · {text(duration(run.duration))}</p><p class="muted">Run ID: <code>{text(run.run_id)}</code></p>')
    if run.truncated:
        body += warnings(["Partial history: only the newest 1,000 journal records are shown. "
                          "Earlier tasks, output, and start times may be missing; "
                          "an incomplete status is not proof of failure."])
    if run.result:
        body += f"<p>Systemd result: {text(run.result)}</p>"
    body += '<h2 id="tasks" tabindex="-1">Tasks</h2>'
    targets = task_log_targets(logs)
    rows = []
    for task in run.tasks:
        name = (link("#" + targets[task.name], task.name) if task.name in targets else
                text(task.name) + ' <span class="muted">(no retained log records)</span>')
        rows.append((name, status(task.status), text(duration(task.duration))))
    body += table(("Task", "Status", "Duration"), rows) if rows else "<p>No task records available.</p>"
    if run.error:
        body += f'<h2>Error</h2><pre>{text(shorten(run.error, 8000, "error"))}</pre>'
    command = f"floe logs --name {shell_arg(run.workflow)} {shell_arg(run.run_id)}"
    return (body + "<h2>Logs</h2>" + log_view(logs)
            + f'<p class="muted">Unabridged logs: <code>{text(command)}</code></p>')
