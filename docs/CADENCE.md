# Expectation clocks for the evidence loop

The forward-evidence loop runs on two monthly crons — `monthly-freeze.yml`
(the 1st, 13:19 UTC) and `monthly-forward-backtest.yml` (the 6th, 02:41 UTC) —
and until this machinery existed, neither had a cadence watcher. Worse, the
backtest loop was *silent by construction* in immature months: `ledger-declare`
and the report copy ran only for profiles whose panels had matured, and the
commit step no-ops when nothing changed. Before the first matured window, a
silently disabled workflow left exactly the same trace as a healthy one:
nothing. `stock_grader/cadence.py` closes that with two pieces.

## 1. The monthly accounting artifact (written unconditionally)

Every `monthly-forward-backtest` run — matured, immature, or refusing — writes
and commits `docs/forward/<YYYY-MM>/accounting.json`. "The loop ran and
attested that nothing matured" is a recorded fact, not silence: attest false
over optimistic true.

Shape (`schema_version 1.0`):

```json
{
  "schema_version": "1.0",
  "month": "2026-08",
  "runs": [
    {
      "run_utc": "2026-08-06T02:44:10Z",
      "event": "schedule",
      "run_id": "17552341234",
      "profiles": {
        "all_weather": "not_matured",
        "deep_value": "refused_build"
      },
      "counts": {"evaluated": 0, "not_matured": 8, "refused": 1}
    }
  ]
}
```

Rules, all enforced by `tests/test_cadence.py`:

- **One file per month, one entry per run.** A re-dispatch inside the same
  month appends to `runs`; no run's record is ever erased. An existing file
  that fails to parse, names a different month, or carries an unknown
  `schema_version` is refused, never clobbered.
- **States are a closed vocabulary** — `evaluated`, `not_matured`,
  `refused_build`, `refused_declare`, `refused_backtest`. No free-text refusal
  reasons: detail stays in the workflow log, so a licensed derived number
  (Massive closes, delisted histories) is structurally unable to reach this
  public artifact no matter what a refusal message interpolates.
- The workflow writes states from every branch of its per-profile loop and
  calls `stock-grader forward-accounting` *after* the loop, before
  `exit $failed` — an accounting refusal is itself a red run.

## 2. `stock-grader check-cadence` — expectation clocks, not age clocks

Modeled on the vault's `check_market_eod` (expectation-based, with
`--pre-collection`-style grace), not on artifact-age thresholds:

| Clock | Expectation | Grace day |
|---|---|---|
| accounting | `docs/forward/<YYYY-MM>/accounting.json` exists for the current month | day **8** (cron fires the 6th; two days for GitHub cron jitter/skips) |
| freeze | newest `<root>/<profile>/<YYYY-MM-DD>.parquet` is from the current month, for **every** committed evidence root (`frozen_scores`, `frozen_scores_wide`) | day **4** (cron fires the 1st) |

The freeze clock is evaluated once per root and fails if ANY root is stale:
monthly-freeze writes both trees on the same cron and commits both, and one
healthy tree must never vouch for a stalled sibling. `--frozen-root` is
repeatable if a caller needs a different set.

Before a clock's grace day, the previous month satisfies it. Both clocks are
derived from artifact filenames, never mtimes. Both are **bootstrap-guarded**:
no artifacts at all is a pass with a note, so the gate could land before the
first accounting artifact existed. Exit 1 on a miss — in a scheduled workflow
the red run's failure email is the alerting, exactly like every other clock in
the ecosystem.

`--pre-run` is self-gate mode: the gated run is the one that writes the
current month's accounting, so the accounting clock is held to the previous
month regardless of day. The freeze clock is unaffected — the panels the run
consumes must already be frozen. This closes `monthly-freeze`'s documented
lack of a staleness gate with a lag of days; the previous backstop was the
paper trader's ~45-day `MAX_PANEL_AGE_DAYS` interlock.

## 3. Where the clock is checked

- **Self-gate** at the top of `monthly-forward-backtest.yml`
  (`check-cadence --repo-root . --pre-run`), after the ledger-chain gate.
  Collectors-workflow pattern: a red gate pages, and the build step still runs
  (`!cancelled() && chain succeeded`) so the run self-heals by writing this
  month's accounting. The build step never runs past a broken ledger chain.
- **Cross-coverage**, weekdays, from Stock-Vault's `shadow-arms.yml`, which
  already checks out Stock-Grader:

  ```
  python grader/src/stock_grader/cadence.py check --repo-root grader
  ```

  This is why `cadence.py` is **stdlib-only with no relative imports and must
  stay runnable as a bare script** — the vault does not install the grader
  package. `tests/test_cadence.py` pins both properties (AST scan + a
  subprocess run of the exact invocation); the vault's
  `tests/test_ops_hardening.py` pins the step's presence and placement (after
  the replay, so a stale grader clock pages without costing a day of shadow
  evidence).

## Failure walk-through

If the forward loop's cron dies in August: the vault leg goes red at the
first shadow-arms run on or after August 8 (accounting clock past its grace
day with July still the newest accounted month), and the next monthly-forward
run — however it is revived — goes red on its own `--pre-run` self-gate
because August was never accounted, while still writing its own month's
accounting in the same run. If monthly-freeze dies instead, both the self-gate
and the vault leg go red from day 4. Recovery never requires editing history:
missed months simply remain unaccounted (the gap **is** the record), and the
clocks look only at the newest month.
