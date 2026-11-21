# Floe intake

`server.js` is the Intake agent. It takes a title and description, starts a Pi
Durable session, and asks the model to refine the draft and check for duplicates
before publishing to `ipdelete/floe`. The prompt uses `search_features` and
`create_feature_issue`, with guarded `bash` available for GitHub CLI help and
additional issue inspection. Publishing still goes through `create_feature_issue`.

Requires Node.js 22.19.0+, Pi with a saved `/login`, and GitHub CLI signed in
with permission to create issues in `ipdelete/floe`. Intake reuses `PI_PROVIDER`
and `PI_MODEL` when set; otherwise it uses `github-copilot` / `gpt-6.2-sol`.
`INTAKE_MODEL` can override the model ID.

```sh
cd tools/intake
npm ci --ignore-scripts
npm start
```

```sh
curl -sS http://127.0.0.1:8765/intake \
  -H 'Content-Type: application/json' \
  -d '{"title":"Skip workflow tasks","description":"Let a workflow skip a task based on an earlier result."}'
```

The local endpoint waits for the agent, then returns the session ID and either
`issue_url` (created) or `answer` (the agent's reply). Sessions are stored in
`.intake/`, and new issues include the session ID. Keep the service on loopback
and submit only text suitable for a public issue. If a request errors, check
GitHub before retrying; there is no automatic recovery or deduplication guarantee.

## Bash guard (backlog-agent groundwork)

`bash-guard.js` provides a Pi Durable `beforeTool` hook applied to intake's
`bash` tool. Bash runs locally from the Floe repository root, using the server's
inherited environment and GitHub CLI authentication. The endpoint still takes a
title and description; this does not yet add a general backlog conversation.
Bash and any helpers the agent uses must be installed on the host.

The hook parses Bash with the prebuilt Tree-sitter WASM parser and blocks calls
it cannot understand or approve. It allows:

- `gh` with a literal built-in command: `issue`, `api`, `pr`, `search`, `project`,
  `label`, `repo`, `help`, `--help`, or `--version`.
- JSON/text helpers: `jq`, `grep`, `rg`, `head`, `tail`, `sort`, `uniq`, `cut`,
  `tr`, `wc`, `printf`, `echo`, and restricted `sed`/`awk`.
- Pipelines, sequential commands, `&&`/`||`, simple lowercase variable
  assignments, simple variable expansions, and recursively checked `$(...)`.
- Single-quoted heredoc delimiters for literal multiline input, output to
  `/dev/null`, and `2>&1`.

`sed` accepts one literal print/range (such as `-n 5,24p`) or
`s/old/new/g`-style substitution on stdin. `awk` accepts only literal field
selection, such as `'{print $2}'` or `'{print $1, $2}'`, on stdin. `rg` and
`sort` accept a limited set of literal options, excluding program-launching
options. `printf` requires a literal format and cannot assign variables.

Unknown executables, dynamic executable/gh command names, aliases/extensions,
environment assignments, loops, functions, background jobs, backticks, process
and arithmetic substitutions, and arbitrary file redirections are blocked.
Rejection messages tell the agent what to reformulate.

**This is a guardrail, not a sandbox or a GitHub authorization policy.** Allowed
commands can still read files through their arguments, and `gh api` can mutate
GitHub. The hook does not isolate the host, constrain credentials or repositories,
or control subprocesses launched internally by approved programs. Host binaries
and the inherited environment must be trusted. Other tools are unaffected.

Run the tests with `npm test` from `tools/intake`. They use the real Durable
harness, Bash, `gh --version`, and shell helpers with a scripted model; they do
not need a model login or make GitHub network requests. The WASM dependency
requires no native build or npm install scripts.
