# Planted-IC power table — 2026-08-03

Calibration of the monthly forward-backtest significance gate (`stock-grader backtest`: PSR/DSR + bootstrap Sharpe CI, `significant` = DSR >= 0.95 AND CI low > 0) against fully synthetic panels with a KNOWN planted cross-sectional rank IC.

## Provenance

- Grid: `backtest calibration panels (known planted rank IC)` generated 2026-08-03T20:37:49+00:00 (generator commit `c47b53041a3622d7506b48c41485a5e5499425b4`), 100% synthetic (no real or licensed market data; the raw grid stays in its source repo).
- Evaluator: `stock-grader backtest` at Stock-Grader commit `8ff7f2b` with the exact monthly-forward-backtest.yml flags: `--periods-per-year 12 --min-cross-section 20 --quantiles 5 --allow-unverified-panel`.
- One fresh scratch ledger per replication: DSR deflates a single trial (E[max] benchmark 0, so DSR == PSR). The production ledger accumulates trials monthly, so production deflation is at least as harsh and true power is <= every number below.
- Every input parquet was verified against the grid manifest's sha256 before evaluation.
- Sampling noise: most cells use 20 replications (100 for the 3/6-month null cells), so a tabulated rate carries a binomial standard error of up to ~0.11. Adjacent cells can invert — e.g. planted 0.02 vs 0.03 at 12 months / 250 names, where the grid's realized mean rank ICs themselves inverted (0.026 vs 0.024 per its manifest). Read trends, not single cells.

## False-positive rate at planted IC = 0

Fraction of null replications the gate passed (target: <= alpha = 0.05).

| months | universe 250 | universe 1000 | seeds |
|---|---|---|---|
| 3 | 0.00 | 0.00 | 100 |
| 6 | 0.00 | 0.00 | 100 |
| 12 | 0.00 | 0.00 | 20 |
| 36 | 0.00 | 0.00 | 20 |

## Detection power, universe 250

Fraction of replications the gate passed at each planted rank IC.

| planted rank IC | 3 mo | 6 mo | 12 mo | 36 mo |
|---|---|---|---|---|
| 0.01 | 0.00 | 0.00 | 0.00 | 0.10 |
| 0.02 | 0.00 | 0.00 | 0.25 | 0.15 |
| 0.03 | 0.00 | 0.00 | 0.15 | 0.45 |
| 0.05 | 0.00 | 0.00 | 0.65 | 0.95 |

## Detection power, universe 1000

Fraction of replications the gate passed at each planted rank IC.

| planted rank IC | 3 mo | 6 mo | 12 mo | 36 mo |
|---|---|---|---|---|
| 0.01 | 0.00 | 0.00 | 0.00 | 0.10 |
| 0.02 | 0.00 | 0.00 | 0.20 | 0.40 |
| 0.03 | 0.00 | 0.00 | 0.65 | 1.00 |
| 0.05 | 0.00 | 0.00 | 1.00 | 1.00 |

## Gate anatomy

`significant` requires BOTH DSR >= 0.95 AND bootstrap CI low > 0. The bootstrap CI (`block_bootstrap_sharpe_ci`, block = 10) returns (0, 0) whenever there are fewer than 11 periods, so at 3 and 6 months the CI-low condition can NEVER hold and the gate is structurally closed regardless of signal strength. Below 30 periods the verdict string additionally reads INSUFFICIENT SAMPLE.

| months | universe | planted IC | DSR >= 0.95 rate | CI low > 0 rate | gate rate |
|---|---|---|---|---|---|
| 3 | 250 | 0.00 | 0.02 | 0.00 | 0.00 |
| 3 | 250 | 0.01 | 0.05 | 0.00 | 0.00 |
| 3 | 250 | 0.02 | 0.10 | 0.00 | 0.00 |
| 3 | 250 | 0.03 | 0.05 | 0.00 | 0.00 |
| 3 | 250 | 0.05 | 0.15 | 0.00 | 0.00 |
| 3 | 1000 | 0.00 | 0.01 | 0.00 | 0.00 |
| 3 | 1000 | 0.01 | 0.00 | 0.00 | 0.00 |
| 3 | 1000 | 0.02 | 0.05 | 0.00 | 0.00 |
| 3 | 1000 | 0.03 | 0.15 | 0.00 | 0.00 |
| 3 | 1000 | 0.05 | 0.35 | 0.00 | 0.00 |
| 6 | 250 | 0.00 | 0.03 | 0.00 | 0.00 |
| 6 | 250 | 0.01 | 0.00 | 0.00 | 0.00 |
| 6 | 250 | 0.02 | 0.10 | 0.00 | 0.00 |
| 6 | 250 | 0.03 | 0.20 | 0.00 | 0.00 |
| 6 | 250 | 0.05 | 0.25 | 0.00 | 0.00 |
| 6 | 1000 | 0.00 | 0.02 | 0.00 | 0.00 |
| 6 | 1000 | 0.01 | 0.05 | 0.00 | 0.00 |
| 6 | 1000 | 0.02 | 0.05 | 0.00 | 0.00 |
| 6 | 1000 | 0.03 | 0.45 | 0.00 | 0.00 |
| 6 | 1000 | 0.05 | 0.85 | 0.00 | 0.00 |
| 12 | 250 | 0.00 | 0.00 | 0.05 | 0.00 |
| 12 | 250 | 0.01 | 0.00 | 0.25 | 0.00 |
| 12 | 250 | 0.02 | 0.25 | 0.50 | 0.25 |
| 12 | 250 | 0.03 | 0.15 | 0.55 | 0.15 |
| 12 | 250 | 0.05 | 0.65 | 1.00 | 0.65 |
| 12 | 1000 | 0.00 | 0.00 | 0.00 | 0.00 |
| 12 | 1000 | 0.01 | 0.00 | 0.10 | 0.00 |
| 12 | 1000 | 0.02 | 0.20 | 0.45 | 0.20 |
| 12 | 1000 | 0.03 | 0.65 | 0.90 | 0.65 |
| 12 | 1000 | 0.05 | 1.00 | 1.00 | 1.00 |
| 36 | 250 | 0.00 | 0.00 | 0.00 | 0.00 |
| 36 | 250 | 0.01 | 0.10 | 0.20 | 0.10 |
| 36 | 250 | 0.02 | 0.20 | 0.20 | 0.15 |
| 36 | 250 | 0.03 | 0.50 | 0.55 | 0.45 |
| 36 | 250 | 0.05 | 0.95 | 0.95 | 0.95 |
| 36 | 1000 | 0.00 | 0.00 | 0.00 | 0.00 |
| 36 | 1000 | 0.01 | 0.10 | 0.15 | 0.10 |
| 36 | 1000 | 0.02 | 0.55 | 0.45 | 0.40 |
| 36 | 1000 | 0.03 | 1.00 | 1.00 | 1.00 |
| 36 | 1000 | 0.05 | 1.00 | 1.00 | 1.00 |

## How to read the September 2026 verdicts

At 3 and 6 matured periods the gate cannot pass at all — the bootstrap CI is undefined below 11 periods and returns (0, 0), so `significant` is structurally false; a September 2026 verdict of NO EDGE or INSUFFICIENT SAMPLE at ~3 periods is therefore NOT evidence that the scores lack skill (under-reading risk: do not kill a profile on it). With >= 50% detection probability as the bar, the smallest detectable planted rank IC is 0.03 at 12 months and 0.03 at 36 months for a 1000-name universe (0.05 and 0.05 for 250 names). Conversely, the worst observed false-positive rate at planted IC = 0 was 0.00 across every null cell, and these numbers are an UPPER bound on production power because each replication here is deflated as a single-trial ledger while the real ledger's multi-trial E[max] benchmark only rises (over-reading risk: a future PASS at 12+ periods on a small universe should still be checked against this table's power at the realized IC, not celebrated as proof of a large edge — a gate this underpowered mostly passes lucky draws of genuinely large signals).
