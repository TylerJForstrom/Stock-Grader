# Stock-Grader

Grades a stock **A+ … F** from fundamentals, statistics and risk — and, more to the point, lets you
change *how* it grades. Metrics roll up into pillars, pillars roll up into a grade, and at **both**
levels you choose from 23 interchangeable weighting methods. A grade that survives all of them is a
much stronger claim than one from a single hand-picked weight vector.

```
                                    ┌──────────── weighting method (23) ────────────┐
SEC EDGAR ──▶ metrics (105) ──▶ normalize (10) ──▶ PILLARS ──▶ weighting method ──▶ GRADE
                                    └──── aggregator (8) ────┘        (again)        + interval
                                                                                     + explanation
```

```
$ stock-grader grade AAPL --explain

╭─ stock-grader ───────────────────────────────────────────────────────────────╮
│ AAPL  A-  79.7/100   90% CI [77–82]   93th pct                               │
│ profile all_weather  ·  weighting equal/fixed  ·  norm robust_z              │
│ coverage 100%  (42 computed, 0 missing, 0 n/a for sector)                    │
│                                                                              │
│  pillar            score                                 weight    contrib   │
│  ─────────────────────────────────────────────────────────────────────────   │
│  profitability      75.4   ██████████████████░░░░░░         18%      +6.17   │
│  efficiency         73.0   ██████████████████░░░░░░          5%      +1.56   │
│  health             62.8   ███████████████░░░░░░░░░         16%      +2.77   │
│  quality            55.4   █████████████░░░░░░░░░░░         14%      +1.01   │
│  growth             51.3   ████████████░░░░░░░░░░░░         14%      +0.25   │
│  shareholder        50.7   ████████████░░░░░░░░░░░░          7%      +0.07   │
│                                                                              │
│  strongest                             weakest                               │
│  interest_coverage             +0.89   payout_ratio                   -0.37  │
│  fcf_to_debt                   +0.84   cash_conversion                -0.29  │
│  croic                         +0.82   revenue_growth_consistency     -0.25  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

## Install

```bash
git clone git@github.com:TylerJForstrom/Stock-Grader.git
cd Stock-Grader
python3.13 -m venv .venv && .venv/bin/pip install .
export STOCK_GRADER_CONTACT="you@example.com"   # SEC asks for a contact in the User-Agent
```

## Use

```bash
stock-grader grade AAPL --explain              # graded against a bundled 82-name peer set
stock-grader grade AAPL --profile value        # same company, value investor's lens
stock-grader grade AAPL --weighting entropy    # let the data decide the weights
stock-grader rank --universe mylist.txt --profile quality --top 20
stock-grader consensus AAPL                    # every profile at once, plus disagreement
stock-grader methods                           # what's available
stock-grader metrics --pillar valuation
```

## The idea

### Two levels of weighting, one mechanism

You asked for methods that weight each aspect of a stock, then weight the aspects against each
other. That is one registry applied twice: `metrics → pillar`, then `pillars → grade`. Every method
works at both levels.

| family | methods | weights come from |
|---|---|---|
| a priori | `equal`, `fixed`, `ahp`, `rank_order_centroid` | judgement, stated up front |
| dispersion | `entropy`, `critic`, `stddev`, `coefficient_of_variation` | how much a metric *discriminates* |
| structure | `pca`, `inverse_variance`, `inverse_volatility`, `risk_parity`, `min_variance`, `max_diversification`, `hrp`, `decorrelated` | the covariance geometry |
| supervised | `ic`, `ic_ir`, `regression`, `shapley`, `mutual_information` | what actually predicted returns |
| meta | `consensus`, `bagged` | the median across methods; bootstrap-stabilised |

Why so many? Because they disagree, and the disagreement is information. Run the same universe
through eight of them:

```
  method             AAPL     GE    JNJ    JPM     SO    SPG    WMT    XOM
  equal                B+     B-     C+      B     D-      D      C     C-
  entropy              B-     B+      C     A-     D-      D     C-     C+
  critic               B+      B     C+     B-     D-      D      C     C-
  hrp                  B+     B-     C+      B     D-     C-      C     D+
  decorrelated          B     B+     B-     C+     D-      D      C     C-

  score spread:  SO 2.8  ·  WMT 7.9  ·  AAPL 12.2  ·  XOM 21.9  ·  JPM 23.2
```

SO grades D− no matter how you weight it. JPM swings 23 points. That tells you something no single
number does.

`ahp` deserves a mention: you supply pairwise judgements ("valuation matters 3× as much as
momentum"), it takes the principal eigenvector, and it **audits your judgements** — if you claim
A > B, B > C and C > A, the consistency ratio exceeds 0.10 and it says so rather than quietly
averaging your contradiction into a number.

### Compensation is a dial, not an accident

Can a strength cover a weakness? A plain average says *completely* — 100/100/0/0 scores the same 50
as 50/50/50/50. Those are not the same company. The aggregator is a power mean whose `rho` makes
that explicit:

| rho | 1.0 | 0.5 | 0.0 | −1.0 |
|---|---|---|---|---|
| balanced 50/50/50/50 | 50.0 | 50.0 | 50.0 | 50.0 |
| lopsided 100/100/0/0 | 50.0 | **25.0** | **0.0** | **0.0** |

`deep_value` runs at `rho = −0.5`, making solvency nearly a veto — the strategy dies if the cheap
thing doesn't survive. `growth` runs at `0.8`, letting a real compounder look expensive.

### Eleven profiles, and their disagreement is the output

```
$ stock-grader consensus AAPL XOM SO

  AAPL: B- (61.4) — best as low_volatility (79), worst as deep_value (39), clarity  0/100
  XOM:  C+ (55.7) — best as deep_value    (79), worst as growth      (26), clarity  0/100
  SO:   D  (32.7) — best as deep_value    (45), worst as momentum    (24), clarity 46/100
```

AAPL and XOM are near-mirror images. Averaging them both to "C+" would destroy the only interesting
thing here, so `clarity` is reported as a first-class number: low clarity means *the answer depends
entirely on what you're looking for*.

### It tells you when not to trust it

Every grade carries a 90% confidence interval from resampling both the weights (Dirichlet) and the
metric set (leave-p-out), widened as data coverage falls. Metrics that disagree produce a wide
interval — `[61.5, 77.9]` versus `[70.0, 70.6]` when they agree. Below 35% coverage it refuses to
issue a letter at all, and grading with no peer universe returns `N/A` rather than a confident-looking
`C 50.0` built from nothing.

## Data

**Fundamentals: SEC EDGAR XBRL.** Free, official, no API key, and it carries the real `filed` date
of every fact — so `--pit` gives *genuine* point-in-time backtesting rather than the usual "lag
everything 45 days" approximation.

**Prices: derived from SEC filings.** No free price *feed* was reachable (Yahoo 429s, Stooq serves a
bot-check) — so prices come out of SEC filings instead, and no API key is needed for those either.

Every Form 4 reports the per-share price of an insider's trade. Open-market sales, purchases and
tax-withholding transactions all execute at the prevailing market price, and SEC publishes them
quarterly as an ~8 MB bundle covering **~3,000–4,000 tickers**. Measured against known market ranges,
the median insider price landed inside the real range for every one of eight test companies.

`dei:EntityPublicFloat` is the fallback: a dollar market value with a measurement date on every 10-K
cover. It *excludes affiliate holdings*, so dividing by all shares understates — by 50% for Walmart
(the Waltons hold about half) and 37% for Simon Property, always in the direction that makes a stock
look cheap. So it is only used with the affiliate fraction solved from a **date-matched** insider
price, which recovers 49.9% for Walmart and 95% for widely-held names.

Sources are ranked by **freshness, not kind** — a 131-day-old insider price beats a 480-day-old
float — and every grade reports the price's age.

These prices are sparse (a few dates per quarter), which is enough for valuation but not for
volatility, beta or momentum.

**For the daily statistics**, `--stockanalysis` fetches split- and dividend-adjusted daily OHLCV
and brings all 40 risk/momentum/liquidity metrics to life (coverage goes to 100%). It is **opt-in
by design**: an undocumented internal endpoint of a commercial site, not a licensed feed. Its
robots.txt disallows nothing for general agents and no access control is circumvented, but read
their Terms of Service before depending on it.

The adjustment is verified rather than assumed — BRK.B (never paid a dividend) has adjusted and raw
closes identical on 100% of bars, while AT&T's ten-year price CAGR is −5.5% against +3.1% adjusted.
Using the raw close would report a decade of AT&T as a loss.

Alternatively supply your own, with no caveat attached:

```bash
stock-grader grade AAPL --price AAPL=232        # one number, unlocks every valuation metric
stock-grader grade AAPL --price-dir ./prices    # TICKER.csv files; date,close is enough
```

`yahoo` and `tiingo` providers ship and will likely work on a normal network — they just could not
be verified here, so they fail soft with a warning rather than taking down the grade.

Details and measurements: [`docs/design/DATA-GROUND-TRUTH.md`](docs/design/DATA-GROUND-TRUTH.md).

## Four traps this handles that most implementations don't

Each was found against live filings, and each produced a *silently wrong* number rather than an error.

1. **There is no Q4 10-Q.** Apple's FY2024 revenue is 391.04B; the three quarterly records sum to
   296.11B. Naive summing understates trailing revenue by **24%**. Q4 is derived as `FY − Q1 − Q2 − Q3`.
2. **`fy`/`fp` describe the filing, not the fact.** Apple's history carries a fiscal-2022 period
   stamped `fy=2024`. Periods come from `start`/`end` only.
3. **Share counts are averages, not flows.** Q4-deriving them by subtraction gave Apple **−30 billion
   shares**, which flowed straight into market cap and corrupted every multiple.
4. **A bank has no current ratio.** JPMorgan files no `GrossProfit`, `AssetsCurrent` or inventory —
   not missing data, but undefined quantities. Metrics resolve to `OK` / `MISSING` /
   `NOT_APPLICABLE`, and only `MISSING` costs coverage, so financials aren't handed artificially
   uncertain grades. Altman-Z is disabled for them outright: it was never estimated on a bank.

And the one that matters most: **a loss-making company has no P/E**, not a negative one. Sorted
"cheapest first", an unguarded negative P/E puts the most distressed names at the top of your value
screen. Every valuation multiple passes `positive_denominator=True`.

## Layout

```
src/stock_grader/
  types.py          core dataclasses; the Coverage three-state distinction
  registry.py       decorator-based plugin registries
  data/
    sec.py          EDGAR client + XBRL normalisation (the four traps)
    concepts.py     canonical concepts and their tag fallback chains
    sectors.py      SIC classification + metric applicability matrix
    prices.py       pluggable price providers, all fail-soft
    synthetic.py    labelled generated data for offline tests
  metrics/
    util.py         guarded arithmetic — returns None, never a misleading zero
    fundamental.py  62 metrics across 7 pillars
    statistical.py  40 risk / momentum / time-series metrics
    models.py       Beneish M, Ohlson O, Altman Z'' with published coefficients
    engine.py       evaluation + three-state coverage
  normalize.py      10 normalizers, cross-sectional and absolute
  aggregate.py      8 aggregators incl. the CES compensation dial
  weighting.py      23 weighting methods
  scoring.py        grade scale, uncertainty, contribution decomposition
  pipeline.py       orchestration
  profiles.py       11 investment styles + consensus
  cli.py / report.py
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

Property-based invariants (weights sum to 1 for every method; improving any metric never lowers the
grade; missing data renormalises rather than dragging toward zero; permuting metric order changes
nothing; grades are deterministic under a seed), plus regression tests pinning each of the four data
traps above, plus a planted-signal recovery test — a weighting method that can't find a known signal
in generated data is broken, and that's only testable against data whose truth you control.

## Design notes

`docs/design/` carries the merged specification (`SPEC.md`, `METRICS.md`, `WEIGHTING.md`,
`MANIFEST.json`) produced by a multi-agent design pass, plus `DATA-GROUND-TRUTH.md` — the measured
data-source findings, which override the others wherever they disagree.

Treat the spec documents as a **design backlog, not a description of the code**. They propose 33
weighting methods against the 23 implemented, and raise open questions (universe bootstrap in the
confidence interval, sector×size calibration keying, Choquet interaction priors) that are not built.
Where a spec claim was checkable it was checked; one of its catches was real and is fixed — see
`short_term_reversal_1m`.

## Caveats

- **Not investment advice.** A grade is a summary of published numbers, not a recommendation.
- Cross-sectional grades are **relative to the universe you pass**. The bundled peer list is a
  convenience, not an index — index membership is licensed data this project can't ship.
- The supervised weighting methods need forward returns. Feeding them returns that overlap the
  metric window is look-ahead bias; the grade will look brilliant and mean nothing.
- Price-derived pillars are only as good as the price data you supply.
