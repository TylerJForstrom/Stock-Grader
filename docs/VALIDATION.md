# Leakage-aware validation and backtesting

Stock-Grader includes a CLI and Python evaluator for a prepared historical score panel. It does not
include a survivorship-free security master, licensed total-return database, point-in-time panel
builder, trained forecasting model, or published out-of-sample result.

No predictive-accuracy claim should be made until those inputs and an independent evaluation
protocol exist.

## The evaluation unit

One row represents a score frozen at a signal date and a strictly later outcome:

| Column | Core evaluator | Strict CLI | Contract |
|---|:---:|:---:|---|
| `signal_date` | yes | yes | Date on which every score input was knowable |
| `return_start` | yes | yes | Must be strictly after `signal_date` |
| `return_end` | yes | yes | Must be strictly after `return_start` |
| `ticker` | yes | yes | Row identifier used by the evaluator |
| `score` | yes | yes | Frozen finite model score |
| `forward_return` | yes | yes | Decimal holding-period total return, not below -100% |
| `filed_through` | no | yes | Populated for every row and no later than `signal_date` |
| `universe_is_pit` | no | all true | Attests that membership is point-in-time |
| `return_is_total` | no | all true | Attests that distributions are included |
| `delisting_return_included` | no | all true | Attests that delistings/total losses are retained |
| `cik`, `security_id`, or `permanent_id` | no | one populated | Permanent issuer/security evidence |

Duplicate `signal_date`/`ticker` rows and mixed return windows within one signal date are rejected.
Rows with non-finite score or return are dropped.

Attestation values are true only when every non-null value is one of `1`, `true`, `yes`, or `y`
(case-insensitive after string conversion). A successful contract check is evidence supplied by
the caller, not independent verification of the claim.

The evaluator currently uses `ticker` for duplicate detection and portfolio-turnover membership;
the additional permanent-ID column is contract evidence, not the grouping key. Normalize the
operational `ticker` field to a stable security key upstream when historical symbol changes would
otherwise create false turnover, and retain the display ticker separately.

## CLI

```bash
stock-grader backtest frozen-score-panel.parquet \
  --quantiles 5 \
  --min-cross-section 50 \
  --periods-per-year 12 \
  --transaction-cost-bps 20 \
  --bootstrap-samples 2000 \
  --bootstrap-block-periods 6 \
  --seed 7 \
  --format md
```

CSV, Parquet, and `.pq` inputs are supported. By default the command refuses to report when any
strict input-contract item is false.

```bash
stock-grader backtest exploratory.csv \
  --allow-unverified-panel \
  --format json
```

The override permits missing evidence columns, false attestations, or absent permanent-ID evidence
for an explicitly caveated exploratory run. It does not bypass the core validations: later filing
cutoffs, invalid or null dates in a supplied date column, duplicate
signal/ticker rows, returns below -100%, mixed windows within a signal date, or an unusable
cross-section still fail. The report retains every failed contract flag and associated limitation.

## Python API

```python
import json
import pandas as pd

from stock_grader.backtest import BacktestConfig, evaluate_walk_forward

panel = pd.read_parquet("frozen-score-panel.parquet")
config = BacktestConfig(
    quantiles=5,
    min_cross_section=50,
    periods_per_year=12,
    transaction_cost_bps=20.0,
    bootstrap_samples=2_000,
    bootstrap_block_periods=6,
    seed=7,
)
result = evaluate_walk_forward(panel, config)
print(json.dumps(result.to_dict(), indent=2))
```

The Python evaluator reports `input_contract` but does not enforce the CLI's strict rejection
policy. Callers embedding it must decide whether to reject any false contract value.

## Reported diagnostics

For each accepted signal date, the evaluator computes:

- Spearman rank information coefficient between score and forward return;
- equal-weight return for each score quantile;
- top-minus-bottom gross spread;
- set-overlap turnover for top and bottom quantiles;
- a fixed basis-point charge times top plus bottom turnover;
- net spread after that charge.

The aggregate `BacktestReport` includes:

```text
input_contract
observations, rejected_periods
mean_rank_ic, rank_ic_information_ratio, rank_ic_positive_rate
mean_gross_spread, mean_net_spread, spread_positive_rate
annualized_net_spread, annualized_spread_sharpe
max_drawdown, mean_turnover
quantile_monotonicity
rank_ic_interval, net_spread_interval
periods, limitations
```

The intervals are 2.5th–97.5th percentiles of circular moving-block resamples of period means.
They summarize variability within the supplied historical periods. They are not future-return
prediction intervals.

The transaction-cost model is deliberately simple. It does not model bid/ask spreads, nonlinear
market impact, borrow availability, borrow fees, taxes, capacity, execution delay, or trading
halts. Entry from cash into the first top and bottom portfolios is treated as 100% turnover.

## Chronological splits

The helper produces expanding training windows followed by an explicit embargo and test window:

```python
from stock_grader.backtest import purged_walk_forward_splits

splits = list(
    purged_walk_forward_splits(
        panel["signal_date"],
        train_periods=60,
        test_periods=12,
        embargo_periods=3,
        step_periods=12,
    )
)
```

Each `WalkForwardSplit` contains `train_dates`, `embargo_dates`, and `test_dates`.

Important: the helper operates only on ordered signal dates. It does not inspect `return_start` or
`return_end`, and `evaluate_walk_forward` allows outcome windows from different signal dates to
overlap. The caller must choose an embargo that prevents training labels from overlapping the test
information set and must account for the actual forecast horizon.

## Building the forward panel

`stock-grader build-panel` joins a profile's frozen panels to realized forward
returns from the private vault's EOD archive, producing the evaluator's input.

**Placement.** The builder lives in Stock-Grader: it consumes a grader output
(frozen panels) and produces a backtest input, sitting strictly between grader
and backtest in the one-direction DAG, and the attestations it decides must live
beside the evaluator that reads them. Its OUTPUT is restricted (per-row returns
derived from Massive free-tier closes and stockanalysis.com delisted histories),
so panel files go to `build/` (gitignored) and are archived only into the
private vault at `data/backtest_panels/<profile>/`; only aggregate statistics
are committed publicly.

**Emitted columns.** Required: `signal_date`, `return_start`, `return_end`,
`ticker`, `score`, `forward_return`. Contract: `cik`, `filed_through`
(= signal_date — the freeze fetched EDGAR on that day, so no later filing could
have entered the feature set), `universe_is_pit`, `return_is_total`,
`delisting_return_included`. Diagnostics (ignored by the evaluator): `profile`,
`letter`, `percentile`, `coverage`, `config_fingerprint`,
`universe_fingerprint`, `freeze_commit`, `start_close`, `end_close`,
`price_symbol`, `return_source`, `split_factor`, `split_source`,
`terminal_price_used`, `symbol_changed`, `panel_schema_version`,
`builder_commit`.

**Attestations are computed, never hard-coded.**

- `universe_is_pit` — True only when zero rows were dropped for an
  outcome-dependent reason AND every signal date is on or after the forward
  epoch (2026-07-30, the first genuinely forward freeze). A backfilled panel
  against the hand-picked survivor universe is refused outright unless
  `--allow-backfilled-panels`, which forces this attestation False.
- `delisting_return_included` — True only when every vanished name was priced:
  by the exit-day close, else the delisted archive's raw close (column `c`,
  never the site's undocumented adjusted close) inside the window, else the
  last listed close inside the window (`terminal_price_used`, a disclosed
  convention that slightly OVERSTATES returns for names that kept falling
  off-exchange, since the archive excludes OTC). An unresolvable name is
  excluded, counted, and fails this attestation for the whole panel.
- `return_is_total` — **False, always, in v1.** The dividend dataset covers
  three tickers at fiscal-period granularity with no ex-dates on a
  fully-split-adjusted basis, against raw unadjusted closes. Flip only when a
  per-ex-date cash-dividend dataset (ex_date, cash_amount, same unadjusted
  basis) covers >= 99% of panel rows. There is deliberately no flag for it.

**Splits.** The archive stores raw prices, so an uncorrected split fabricates a
~-50% return. Three tiers: a matching foundry `splits.jsonl` row (`foundry`); a
price signature corroborated by the volume signature — share volume scales by
the ratio while trade count stays flat (`reconstructed`, honesty-flagged); and
uncorroborated split-shaped moves, which are EXCLUDED and counted — never kept
(fabricates a crash) and never silently dropped (digs a survivorship hole).

**One hypothesis = one trial.** `trial_sharpes` collapses ledger records to the
most recent per experiment and skips retracted records: re-running one profile
monthly is that hypothesis measured again, not a new trial. Repeated looks are
still optional-stopping; fewer than `--min-periods` (3) qualifying periods
appends no trial at all, because `assess_edge` structurally cannot find
significance below 11 periods and a permanent ledger line for an uninformative
statistic only burns the trial budget.

## Minimum credible dataset construction

### Entity and universe history

- Start with the eligible universe as it was known at every signal date.
- Include delisted, bankrupt, acquired, and merged securities.
- Retain CIK and a security-level identifier in addition to historical ticker.
- Apply listing, liquidity, price, and reporting-history screens using only contemporaneous data.
- Do not select the sample after observing which records have clean future returns.

The SEC's current ticker map is a present-day list and is not sufficient for this.

### Features

- Run filings with `PitMode.PIT`.
- Prove `filed_through <= signal_date` for every observation.
- Freeze tag mappings, normalization parameters, peers, profile, and weights as of the signal.
- Do not use later restatements, revised vendor history, future constituent membership, or
  backfilled estimates.
- Fit learned weights on earlier training dates only.

The pipeline blocks its supervised weighting methods on the same cross-section by default. A
production workflow should learn parameters in the training window, freeze them, and pass them as
fixed weights for the later test window. Setting
`allow_in_sample_supervised_weighting=True` acknowledges leakage; it does not make the result
valid.

### Outcomes

`forward_return` should include:

- cash distributions;
- splits and other corporate actions;
- spin-off and merger consideration;
- delisting return or recovery value;
- an implementable entry and exit convention.

Document whether returns are close-to-close, next-open, volume-weighted, or delayed. The signal
cannot trade at a price observed before it existed.

### Costs and constraints

Use assumptions appropriate to the names and era:

- turnover-dependent commission and spread;
- market-impact and participation limits;
- borrow availability and fee for the short leg;
- position, sector, country, and factor constraints;
- portfolio rebalancing and cash treatment.

The included top-minus-bottom diagnostic is not a deployable portfolio simulation.

## Recommended evaluation sequence

1. Lock the hypothesis, features, profile, universe rule, horizon, and metrics.
2. Reserve an untouched final period.
3. Use expanding or rolling chronological splits.
4. Purge label overlap and apply an embargo.
5. Tune only inside training folds.
6. Report coverage and rejected periods alongside performance.
7. Compare with simple baselines and factor-neutral variants.
8. Report rank IC, quantile monotonicity, spread, turnover, costs, drawdown, and capacity.
9. Break results down by era, sector, size, liquidity, and data-availability cohort.
10. Run the locked pipeline once on the final holdout and report failures as well as successes.

Multiple profiles, horizons, filters, and parameter searches create multiple-testing risk. Keep a
research log and correct expectations for the number of attempted specifications.

## Distress AUC is a diagnostic, not predictive validation

`scripts/validate_distress.py`:

- finds filings using EDGAR text queries;
- computes selected metric values for the labelled issuers;
- compares them with a hand-selected large-cap control group;
- reports contemporaneous rank separation/AUC per metric.

That can reveal a sign inversion or a metric that fails to distinguish an extreme labelled group.
It does not prove that the full grade predicts future distress or returns because:

- text-query labels are not manually adjudicated outcomes;
- the controls are not matched or sampled from the same eligible population;
- filing availability and metric missingness can differ between groups;
- the as-of cutoff can include information contemporaneous with or after the labelled event;
- the exercise is not a chronological train/test design;
- no trading rule, price outcome, cost, or delisting-complete return is evaluated.

An AUC from that script should be described as sample separation for a specific label construction,
date, metric, and control set—nothing broader.

## Sensitivity calibration is also not predictive validation

`scripts/calibrate_intervals.py` compares degraded-data model outputs with the full-data output in
a synthetic test universe. It is an internal robustness test, not evidence of:

- a true-grade coverage probability;
- future-return calibration;
- representative live-market performance;
- peer-selection robustness.

See [Model sensitivity](MODEL-SENSITIVITY.md).

## Reproducibility checklist

Archive:

- code commit and environment lock;
- configuration and random seeds;
- universe and peer manifests;
- source/vintage identifiers and data hashes;
- frozen panel before returns are joined;
- split dates and embargo;
- all attempted specifications;
- complete period-level output;
- license basis for stored and redistributed data.

Without the frozen inputs, a backtest result cannot be independently reproduced or audited.
