---
name: spike
description: Investigate technical uncertainty with focused, disposable experiments and preserve decision-relevant evidence.
---

# Spike

Resolve the repository root from this skill directory (`../../..`). The `.agents/` and `docs/` paths below are relative to that root; use absolute paths when accessing them.

Reduce uncertainty through focused experimentation, identifying what works, what fails, and what remains unknown.

Before experimenting, follow [Floe's spike branch and handoff workflow](../../engineering/README.md#spike-branches-and-handoff). It distinguishes disposable experiment branches from merge-bound work; a branch is sufficient and a spike discussion PR is optional, not intended for merge.

Consult existing evidence and confirm the intended versions before experimenting; define a useful stopping criterion, using a timebox when helpful. Validate the test setup before interpreting failures, using positive controls alongside negative tests where appropriate.

Start by identifying the decision the spike should inform and the smallest useful experiment. Test competing hypotheses when warranted; record actual observations separately from inferences, including failures, limitations, and how results were obtained. Prefer disposable code; do not promote prototype code to production by default. Use `.agents/templates/spike.md` as an optional starting point for durable findings under `.agents/engineering/<effort>/spike.md`; adapt or omit sections as useful. Do not claim tests ran unless they did. Recommend design, further exploration, or stopping based on evidence, not ceremony.

Explain why the experiment mattered and what its findings changed, not just the test results.

## Decision record

When a consequential engineering choice arises, record it contemporaneously in `.agents/engineering/<effort>/decisions.md` using `.agents/templates/decisions.md`; distinguish agent recommendations from engineer-approved decisions. At session end, reconcile meaningful decisions against the work and evidence, explicitly labeling any retrospective reconstruction. Skip trivial choices.
