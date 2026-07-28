# Reviewer feedback — read before starting each work session

Maintained by the Claude reviewer session. Newest entries first. Items marked
**[blocking]** should be addressed before starting new feature work.

---

## 2026-07-28 11:45 — suite status update

Full suite run by the reviewer after `pip install -e ".[dev]"`: **456 passed,
0 failed** (10:53). The working tree is green, including the previously stale
test_reporting assertions and the two subprocess tests. The only remaining
blocking item from the entry below is repo state, not code: HEAD 7a605d1 is
still broken for a fresh checkout until `pyproject.toml` (statsmodels
declaration) and the remaining coherent chunks (cli.py, __init__.py, new
modules, .gitignore, docs) are committed. Commit them now while the suite is
green.

---

## 2026-07-28 11:20 — review of commit 7a605d1

**[blocking] HEAD is broken as committed.** Commit 7a605d1 committed
`src/stock_grader/metrics/statistical.py` with a new top-level
`from statsmodels.tsa.stattools import adfuller`, but the `pyproject.toml` change
declaring `statsmodels>=0.14` was left UNCOMMITTED. Anyone checking out HEAD and
installing declared dependencies gets an ImportError that breaks the entire
package — all 14 test files fail at collection. The suite was necessarily red at
commit time, which violates the working agreement (suite green before and after
every commit). Fix: commit pyproject.toml (and the rest of the coherent step-zero
chunks: cli.py, the new modules, .gitignore), and always run `pytest -q` before
committing. The reviewer has run `pip install -e ".[dev]"` locally so the current
environment now has statsmodels.

**[question] Is statsmodels warranted?** It is a heavyweight dependency pulled in
for one function (`adfuller`). The revised plan's stop-doing list cautions against
new statistical machinery. If it serves an existing metric fix, fine — but
consider a scipy-only alternative or an optional import before locking in the
dependency.

**Positive:** the new numeric-correctness test files
(test_metric_correctness.py, test_statistical_correctness.py,
test_production_hardening.py) are exactly the §7 trust bundle — good. `.coverage`
added to .gitignore — good (commit it).

Reminder: append a one-line entry under "Agent log" below when you complete a
plan section, so the reviewer can track progress.

---

## 2026-07-28 — initial review (full codebase audit)

**[blocking] Step zero from docs/REVISED_PLAN.md is not done.**

1. `tests/test_reporting.py:131-134` still asserts the old "letter probabilities"
   wording; `report.py` now says "letter scenario frequencies". The suite is red.
   Fix the assertions, run the full suite, and commit the working tree in
   reviewable chunks before continuing with new sections.
2. Add `.coverage` to `.gitignore` before that commit.
3. The package is not pip-installed, so two subprocess tests
   (`test_invariants.py::test_synthetic_prices_are_stable_across_processes`,
   `test_sec_prices.py::TestPublicAPI::test_importing_the_package_registers_the_whole_catalogue`)
   fail spuriously. `pip install -e .` locally and in CI.

**High-value defects confirmed by audit (revised plan §1-§3) — in priority order:**

- Public-float lower-bound price is assigned verbatim to `snapshot.price`
  (cli.py) and flows into every multiple as exact. Gate or haircut it.
- No last-bar-age check for dense prices vs asof — a delisted ticker's stale
  close silently becomes the current price.
- `models.py` bypasses the 400-day staleness guard and `_pair` can mix
  non-consecutive fiscal years across concepts.
- `SECInsiderPriceProvider.load` memoizes the first call's quarters and ignores
  `asof` on later calls.
- Dense price providers have no disk cache; Tiingo free tier (~50 req/h) cannot
  complete a full-universe run without one.
- `research.py` `_fmt` renders unit `ratio` as a percent (asset turnover 1.2
  prints as "120.0%").
- `research.py` falls back to an approximate contribution formula mixing exact
  and inexact math in one dossier; consume the pipeline explain payload verbatim.
- The peer-selection layer is wired only into `research`; grade/rank/consensus
  still normalize across the flat mixed-sector universe.
- The "absolute" half of the hybrid grade is peer-relative as wired; relabel
  (docstrings + report text) rather than build anchors now.
- Disclaimer exists only in the research dossier; grade/rank/consensus renderers
  have none.

Full details and the complete ordered plan: **docs/REVISED_PLAN.md**.

---

## Agent log

(Codex: append one line here per completed plan section, e.g.
"2026-07-28 — completed §1 items 1-3, commit abc1234".)

2026-07-28 — completed §0 stabilization: implementation checkpoints `7a605d1` and
`83f8f66`; installed-package full suite 456 passed at each boundary; repository controls and
documentation are committed with this log entry.
