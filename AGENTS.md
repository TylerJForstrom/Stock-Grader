# Instructions for coding agents working in this repo

## Ecosystem

This repo is one member of a multi-project ecosystem. The contract — roles,
artifact rules, data-flow DAG, sequencing — lives in the Stock-Data repo:
**https://github.com/TylerJForstrom/Stock-Data/blob/main/ECOSYSTEM.md**.
Read it before any cross-project work. Key rules: integrate via published
datasets with manifests (never code imports); this repo is the system of
record for grading methodology; data enters only through the Stock-Data
foundry.

## Active work queue

A handoff queue with remaining fixes in execution order lives at
**docs/HANDOFF.md** — when it exists and has open items, work from it first.

## Authoritative plan

The implementation plan for this project is **docs/REVISED_PLAN.md** (a reviewed and
re-prioritized revision of the earlier ten-section plan). Follow it in order; its
priorities supersede the original plan. Key changes from the original:

- Step zero (freeze, fix the broken test, commit, re-baseline) comes before all
  feature work. Do not start a new plan section with a dirty tree or a red suite.
- Several original P0s are already implemented in this tree; work only on the
  named residuals listed in the revised plan.
- Data-integrity fixes (revised plan section 1) outrank all scoring refinements.

## Reviewer feedback loop

A reviewer (Claude, running in a separate session) is auditing this repo on a
recurring schedule and writes findings to **docs/REVIEW_FEEDBACK.md**.

- Read docs/REVIEW_FEEDBACK.md at the start of each session and before starting
  a new plan section. Address items marked **[blocking]** before new feature work.
- When you complete a plan section, note it with a one-line entry under the
  "Agent log" heading at the bottom of docs/REVIEW_FEEDBACK.md so the reviewer
  can verify it.

## Working agreements

- One plan section (or coherent sub-item) per commit. Run the full test suite
  before and after each commit; never leave the suite red between commits.
- Do not add new weighting methods, aggregators, or attribution/interval
  precision — see the "stop doing" list at the end of the revised plan.
- Never commit `.coverage` or other generated artifacts; keep `.gitignore` current.
- All output shown to users must carry the not-investment-advice framing; never
  present grades, valuations, or model output as predictions or recommendations.
