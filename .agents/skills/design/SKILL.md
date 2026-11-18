---
name: design
description: Turn engineering evidence into explicit architectural decisions, trade-offs, constraints, and validation plans.
---

# Design

Resolve the repository root from this skill directory (`../../..`). The `.agents/` and `docs/` paths below are relative to that root; use absolute paths when accessing them.

Convert evidence into explicit architectural decisions, trade-offs, constraints, and a coherent implementation strategy.

Consult relevant spike findings, existing code, and constraints. Distinguish evidence from assumptions; explain consequential alternatives and why they were rejected. Specify invariants and how important assumptions can be validated. Use `.agents/templates/design.md` as an optional starting point under `docs/engineering/<effort>/design.md`; adapt or omit sections as useful. Revisit decisions when implementation or new experiments produce material evidence. Ask the engineer to decide consequential trade-offs rather than silently committing on their behalf.

Link important choices to their supporting findings and reconcile conflicting assumptions.

## Decision record

When a consequential engineering choice arises, record it contemporaneously in `docs/engineering/<effort>/decisions.md` using `.agents/templates/decisions.md`; distinguish agent recommendations from engineer-approved decisions. At session end, reconcile meaningful decisions against the work and evidence, explicitly labeling any retrospective reconstruction. Skip trivial choices.
