# Engineering Knowledge

Floe uses **Spike → Design → Implement** as complementary methods inside its
[PR workflow](../../AGENTS.md#development-and-pull-request-workflow), not mandatory
stages or a replacement for CI/CD. Choose the cheapest useful action that can
change a decision; small, understood changes can proceed straight to implementation.

## Using the skills

Pi supports repository-local `.agents/skills/` discovery. After granting project
trust, start a new session or run `/reload` in an existing one, then invoke:

```text
/skill:spike Investigate whether concurrent workers can preserve Floe's import isolation.
/skill:design Design parallel execution using the spike evidence and explicit trade-offs.
/skill:implement Implement the approved design and flag material new findings.
```

The skills and templates are development instructions, not Floe runtime
requirements. Installation adds no executable hooks, extensions, global settings,
or Python/Node package dependencies. It does not run experiments or authorize
real-systemd operations. All [repository safety rules](../../AGENTS.md#non-negotiable-safety-rules)
and review/merge requirements still apply.

## Artifacts and decisions

Use a directory per engineering effort when durable artifacts are useful, e.g. `docs/engineering/connection-pooling/` with `spike.md` and/or `design.md`.

Record decision-relevant evidence, failed approaches, constraints, assumptions, architectural decisions, invariants, and meaningful implementation discoveries. Separate what was observed from what was inferred. Prefer updating an existing document over creating another one.

Templates in `.agents/templates/` are starting points, not required forms. Small, well-understood changes may need no document. Spike code is disposable by default; preserve knowledge rather than prototypes.

For consequential choices, maintain an effort-local `decisions.md`. Capture decisions as they occur, then reconcile at session end. See `.agents/templates/decisions.md`. The design document presents the current architecture; the decision record preserves why choices were made and how they changed.

## Current efforts

- [Parallel tasks](parallel-tasks/README.md): thread-worker contract probes and
  recommendation for issues #8/#9; execution-model approval remains pending.
- [Task retries](task-retries/decisions.md): approved policy/deployment direction,
  event compatibility, and validation scope for issue #6.

## Source and updates

Installed from [ipdelete/agentic-engineering-method](https://github.com/ipdelete/agentic-engineering-method)
at commit `28897512f5d49f69eb3b9961daba0dfd7f924944`:

- `.agents/skills/{spike,design,implement}/SKILL.md` — upstream skills with an
  added clarification for resolving repository-root-relative artifact paths.
- `.agents/templates/{spike,design,decisions}.md` — unchanged upstream starters.
- This README and the engineering-method section of `AGENTS.md` are adapted to
  Floe; the existing architecture, safety, testing, and PR instructions are retained.

To update, review a pinned upstream revision and its changes, preserve Floe's
local integration and safety rules, and verify skill discovery again. Do not
replace Floe's `AGENTS.md` with the upstream file or add a runtime dependency for
these documentation-only skills.
