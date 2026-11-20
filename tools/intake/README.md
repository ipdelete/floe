# Floe intake

A maintainer-only HTTP service that turns one feature request into a public
issue in `ipdelete/floe`. It uses Pi Durable 1.6.0 with one tool,
`create_feature_issue`. The agent drafts the title and description; the tool
owns the destination, credentials, and session reference. No filesystem or
shell tools are exposed to the agent. This is not a Floe runtime dependency.

## Run locally

Requires Node.js 22.19.0 or later, npm, and Gitleaks 8.30.1 on `PATH`. Load these
values into the environment before launching (don't put secrets in the repo):

- `INTAKE_TOKEN`: a random bearer token, at least 32 characters.
- `GITHUB_TOKEN`: a token scoped to `ipdelete/floe` with Issues read/write.

Intake reuses the local Pi login and cached model catalog. It inherits
`PI_PROVIDER` and `PI_MODEL` when started from Pi, otherwise it defaults to
`github-copilot` / `gpt-6.2-sol`. Set `INTAKE_MODEL` to override the model ID.
If the model is unavailable, check `/login` and `/model` in Pi before starting
Intake. The issue-writing `GITHUB_TOKEN` is separate from the model login.

```sh
cd tools/intake
npm ci --ignore-scripts
npm start
```

The server listens only on `127.0.0.1`; `INTAKE_PORT` defaults to `8765`. Do not
expose it to the internet. Use the same `INTAKE_STATE_DIR` across restarts
(default: `.intake` inside this directory). Keep it private: it contains raw
requests and Pi session transcripts. Do not copy it into the public repo.
Only one service process can own a state directory at a time.

```sh
curl -sS http://127.0.0.1:8765/intake \
  -H "Authorization: Bearer $INTAKE_TOKEN" \
  -H 'Idempotency-Key: feature-001' \
  -H 'Content-Type: application/json' \
  -d '{"request":"Let workflows skip tasks based on earlier results."}'

job_id='replace-with-the-returned-id'
curl -sS -H "Authorization: Bearer $INTAKE_TOKEN" \
  "http://127.0.0.1:8765/intake/$job_id"
```

`POST /intake` accepts a nonblank request of at most 1,000 Unicode code points
in a JSON body of up to 8 KiB. It returns `202` with an ID and a `Location` header. Retrying with the same key and request
returns the same job; different text with that key returns `409`. Status is
`queued`, `running`, `completed` (with the verified issue number and URL), or
`blocked`. The opaque ID on the issue corresponds to `<id>.sqlite` locally;
it is not a link to the raw transcript. Intake does not start Requirements.

## Recovery and privacy

On restart, the worker resumes queued and interrupted jobs using their existing
sessions. It records an attempt **before** sending the GitHub create request. If
it cannot confirm creation, it checks the newest 1,000 issue-list entries for
the session marker (including closed issues). If it still cannot confirm,
`issue_creation_unconfirmed` means **check GitHub before retrying**. Reusing the
same key never sends a second create; use a new key only after confirming the
issue does not exist. Keep the state directory to preserve that guarantee.

The request is scanned before it goes to the model provider, and the draft is
scanned before it goes to GitHub. A finding, missing scanner, or scanner error
blocks the job (`secret_scan_failed`); no finding details are returned. This is
not a general privacy filter: submit only text appropriate for a public issue
after drafting. `no_issue_published` means the agent or session did not produce
a publishable issue (for example, because of a provider failure). Blocked jobs
are terminal and are not picked up at restart. Once a problem that happened
before publication is resolved, submit a fresh request with a new key.

Run `npm test` for local tests. They use a real Pi Durable harness and SQLite,
a scripted model, and a fake GitHub boundary. No credentials are needed.
