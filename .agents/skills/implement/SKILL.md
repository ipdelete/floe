---
name: implement
description: Build and validate software against engineering intent while capturing discoveries that may change the design.
---

# Implement

Resolve the repository root from this skill directory (`../../..`). The `.agents/` and `docs/` paths below are relative to that root; use absolute paths when accessing them.

Build and validate the solution against the design, revisiting decisions and capturing new knowledge as it emerges.

Read relevant design artifacts and repository conventions, then implement the smallest coherent change. Use tests and existing CI/CD checks where available; report what actually ran and what remains unverified. If implementation reveals material new evidence, update the relevant design or flag a decision for the engineer; return to spike or design when warranted. Do not create a separate implementation report by default, and do not turn routine observations into documentation overhead.

When findings change, update affected explanations and links so current understanding stays clear.

## Decision record

When a consequential engineering choice arises, record it contemporaneously in `docs/engineering/<effort>/decisions.md` using `.agents/templates/decisions.md`; distinguish agent recommendations from engineer-approved decisions. At session end, reconcile meaningful decisions against the work and evidence, explicitly labeling any retrospective reconstruction. Skip trivial choices.
