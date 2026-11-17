# feedwatch: let an agent triage your feeds

feedwatch fetches Atom and RSS feeds, asks an agent which new items matter to
you, and publishes its decisions:

- `out/inbox.md`: items to look at today (`notify`), newest first
- `out/digest-<year>-W<week>.md`: items worth a line in a weekly summary (`digest`)
- everything else is recorded as `ignore`

The agent is [pi](https://pi.dev), called once per run in non-interactive mode.
`interests.md` tells it what you care about.

## Requirements

- pi, logged in to a model provider (`pi`, then `/login`), and the Node.js
  that pi runs on. Floe and this workflow need only the standard library.
- Network access to the feeds and to the model provider.

## Set up

```bash
cd examples/feedwatch
cp config.example.json config.json   # then edit it
pi --list-models                     # pick a model for "model"
floe run feedwatch.py                # a first run, directly
```

In `config.json`:

- `pi`: the pi executable. `~` is expanded.
- `node_bin`: the directory containing `node`, or `null`. The service does not
  get your shell's `PATH`, so set this when node comes from a version manager
  such as nvm; `dirname "$(command -v node)"` prints it.
- `model`, `thinking`: the model and thinking level, as pi's `--model` and
  `--thinking` take them.
- `max_items_per_run`: items per agent call. More new items than this wait for
  later runs. `max_age_days` skips older items, which keeps the first run small.
- `feeds`: a name and a URL for each feed. The name is part of each item's
  identity, so renaming a feed makes its items new again.

Then install it, and run it once under systemd to check that pi works there:

```bash
floe install feedwatch.py
floe start feedwatch
floe logs feedwatch       # each decision, with the agent's reason
```

If `preflight` fails under systemd but `floe run` works, read
[The service environment](../../docs/deployment.md#the-service-environment).

## How it works

```
preflight ───────────────┐
fetch ──▶ select ──▶ triage ──▶ publish ──▶ report
```

| Task | What it does |
|---|---|
| `preflight` | Checks that pi exists and has credentials for the model (`pi auth check`). |
| `fetch` | Fetches each feed. A failing feed is recorded and the others continue. |
| `select` | Keeps items not decided before and newer than `max_age_days`, up to `max_items_per_run`. |
| `triage` | Asks pi for one decision per item, validates the reply, and saves the decisions. |
| `publish` | Rebuilds `out/` from all saved decisions. |
| `report` | Logs a summary, and fails the run if a feed failed, naming it. |

State is in `state/`: `decisions.json` holds every decision with its reason,
model, and run ID; `last_reply.txt` is the agent's latest raw reply.

## Safety

Feed content is untrusted: anyone who can publish to a feed can write text the
agent reads, including instructions. feedwatch limits what that text can do:

- pi runs without tools (`--no-tools`), extensions, skills, MCP servers,
  project context files, or a saved session, so the agent can only answer.
- The reply is untrusted too. It must be a JSON array with exactly one
  decision from `notify`, `digest`, `ignore` per item, in order, each with a
  reason. Anything else fails the run, and no decision from it is saved.
- At worst, a feed item can sway how the items in the same call are
  classified.

## Costs and retries

Each run with new items makes one model call; runs without new items make
none. Floe does not retry: after a failure, items stay undecided and the next
scheduled run asks again. Decisions, once saved, are never requested again.
