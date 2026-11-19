# Project skills

Pi discovers skills under `.agents/skills/` after project trust is granted.
Run `/reload` in an existing session to pick up additions.

- `/skill:init` loads the initial repository context. Its three steps are defined
  in [the skill](skills/init/SKILL.md); invocation is explicit, not automatic.
- `/skill:writing-for-agents` uses Matt Pocock's reference for agent-facing
  documents. It is also model-discoverable when writing skills or editing
  `AGENTS.md` or `CLAUDE.md`.
- See [Engineering knowledge](engineering/README.md) for the Spike, Design, and
  Implement methods, artifact layout, and branch/evidence handoff workflow.
- See [Development](development.md) for local validation, CI, and opt-in
  real-systemd tests.

## Writing for Agents: source and updates

Copied from [mattpocock/skills](https://github.com/mattpocock/skills), commit
`5863806dd4e15c63174bee4e7515a4256792ecfa`, directory
`skills/productivity/writing-for-agents/`. The skill, supporting reference, and
`agents/openai.yaml` metadata are unchanged. The upstream MIT license is included
as `skills/writing-for-agents/LICENSE`.

To update, review a pinned upstream revision, copy the complete skill directory,
retain the license, and verify Pi discovery and the supporting reference links.
This repository-local installation adds no plugins, executable hooks, global
settings, or runtime dependencies.

`init` is Floe-specific and was authored using the installed Writing for Agents
reference. Its paths are relative to its skill directory, not the session's
working directory.
