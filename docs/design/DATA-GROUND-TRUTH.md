# Data layer — empirically verified ground truth

> Everything in this document was **measured** from live endpoints on 2026-07-24, not assumed.
> Where a source is unavailable it says so and says what to do instead.
> Implementers: this document overrides any conflicting statement in `SPEC.md` about data sources.

## 1. Source availability (measured)

| Source | Endpoint | Result | Verdict |
|---|---|---|---|
| **SEC EDGAR XBRL** | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | HTTP 200, 3–7 MB/company | **PRIMARY fundamentals source** |
| **SEC submissions** | `data.sec.gov/submissions/CIK##########.json` | HTTP 200 | SIC code, exchange, fiscal year end |
| **SEC frames** | `data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY2024Q1.json` | HTTP 200, 320 KB | Cross-sectional panel in one call |
| **SEC tickers** | `www.sec.gov/files/company_tickers.json` | HTTP 200, 798 KB | ticker ⇄ CIK universe map |
| **FRED** | `fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10` | HTTP 200, CSV | **Risk-free rate** (Sharpe/Sortino/CAPM) |
| Yahoo Finance | `query1/2.finance.yahoo.com/v8/finance/chart/…` | **HTTP 429** on every attempt, with browser UA, with cookie jar, 3 retries | Blocked from this network |
| Stooq | `stooq.com` / `stooq.pl` `/q/d/l/?s=…` | HTTP 200 but body is a **JavaScript proof-of-work bot check**, not CSV | Not usable programmatically |

### What this means

- **Fundamentals: solved.** SEC EDGAR is free, official, keyless, and carries real filing dates.
- **Prices: no free source is reachable from this build environment.** The Yahoo 429 is an
  egress-level block on a shared IP and will very likely **not** apply on a normal home network,
  so the Yahoo provider is still worth shipping — it just could not be verified here. The Stooq
  bot-check is a deliberate anti-automation measure and is **not** to be circumvented.

### Required consequence for the design

The system must be **fully functional with fundamentals alone**, and treat prices as an optional
enrichment. Concretely:

1. Every price-dependent metric declares `needs_prices=True` and is dropped (with weight
   renormalisation, see §5) when no price series is available.
2. Ship a `--price` scalar override and a `prices.csv` drop-in so a user with no price API still
   gets every valuation metric (P/E, EV/EBITDA, …) from a single number they type in.
3. Price providers ship as best-effort plugins: `yahoo`, `stooq`, `tiingo`, `fmp`, `alphavantage`
   (last three keyed via env var). They must fail *soft* — log a warning, return `None`, never raise.
4. The bundled offline fixtures use **real SEC fundamentals** plus a **clearly labelled synthetic
   price series**. Synthetic files live under `data/samples/synthetic/` and every loader that reads
   them stamps `meta["synthetic_prices"]=True`, which the report prints as a visible warning.
   Synthetic data must never be presentable as real market history.

## 2. The `companyfacts` record shape

```json
{"start":"2025-12-28","end":"2026-03-28","val":111184000000,
 "accn":"0000320193-26-000013","fy":2026,"fp":"Q2","form":"10-Q",
 "filed":"2026-05-01","frame":"CY2026Q1"}
```

- Balance-sheet (**instant**) facts have `end` only — no `start`.
- Income/cash-flow (**duration**) facts have both.
- `filed` is the date the number became public. **This is the point-in-time key.**
- `frame` is present only on records SEC could align to a calendar period.

### `fy` / `fp` are NOT the record's own period — verified

From AAPL's revenue history, two records carrying `fy=2024`:

```
2021-09-26 -> 2022-09-24   fp=FY   (this is fiscal 2022!)
2023-01-01 -> 2023-04-01   fp=Q2   (this is a fiscal-2023 quarter)
```

`fy`/`fp` describe **the filing that contained the fact**, not the fact's period. Keying periods off
`fy`/`fp` mislabels history by up to two years.

> **Rule: derive the period exclusively from `start`/`end`. Never trust `fy`/`fp`.**

## 3. The Q4 trap — verified with real numbers

10-Q filings report discrete quarters *and* year-to-date cumulative figures; the 10-K reports only
the full year. Measured duration classes in AAPL's revenue history (`round((end-start)/91.31)`):

```
1 quarter: 62 records    2 quarters: 16    3 quarters: 14    4 quarters: 21
```

So **fewer than half** the records are discrete quarters. And for fiscal 2024:

```
FY2024 10-K  2023-10-01 -> 2024-09-28   391.04B
  Q1 10-Q    2023-10-01 -> 2023-12-30   119.58B
  Q2 10-Q    2023-12-31 -> 2024-03-30    90.75B
  Q3 10-Q    2024-03-31 -> 2024-06-29    85.78B
  ------------------------------------------------
  sum of available quarters              296.11B
  MISSING Q4                              94.93B   (= 391.04 - 296.11)
```

**A naive "sum the quarterly records" TTM understates AAPL revenue by 24%.** There is no Q4 10-Q —
it does not exist for any US filer.

> **Rule: Q4(duration) = FY − Q1 − Q2 − Q3.** Balance-sheet instants need no such treatment.

### Canonical duration-fact normalisation

```
1. Keep USD-unit records.
2. Classify by n_q = round((end - start).days / 91.31)  -> 1, 2, 3, or 4.
3. Discrete quarters  := n_q == 1.
4. For each fiscal year, if the FY record (n_q == 4) exists and only 3 discrete quarters
   fall inside [FY.start, FY.end], synthesise Q4 = FY.val - sum(those three).
5. Cumulative records (n_q in {2,3}) are *differenced* only when discrete quarters are absent:
   Q2 = YTD_2 - YTD_1, Q3 = YTD_3 - YTD_2.
6. Deduplicate by (start, end) — see §4.
7. TTM = sum of the 4 most recent discrete quarters, and it is only valid when all 4 exist.
   Emit coverage=False rather than a 3-quarter TTM.
```

## 4. Restatements and point-in-time selection

The same `(start, end)` period can appear in several filings with different `val` (restatement).
AAPL revenue showed **0** conflicting periods, but this is company-specific and must not be assumed.

Two selection modes, configurable, both required:

- `pit` (**default for backtests**): among records for a period, keep the one with the greatest
  `filed` **that is still ≤ asof**. This is what an investor could actually have known.
- `latest` (default for a single "grade this stock today" call): keep the greatest `filed` overall,
  i.e. the most-restated, most-accurate figure.

Mixing the two inside one grade is a correctness bug — the mode is set once per run and recorded in
`GradeReport.meta["pit_mode"]`.

> The `filed` field makes genuine point-in-time possible. This is strictly better than the
> "lag fundamentals by 45/90 days" heuristic in `SPEC.md`; **the lag heuristic is now a fallback**
> used only by providers that do not supply filing dates.

## 5. Tag variance — fallback chains are mandatory

Measured across 8 companies (AAPL, GE, JNJ, JPM, SO, SPG, WMT, XOM). Number = which position in the
fallback chain matched; `--` = **no tag found at all**.

| concept | AAPL | GE | JNJ | JPM | SO | SPG | WMT | XOM |
|---|---|---|---|---|---|---|---|---|
| revenue | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 0 |
| gross_profit | 0 | 0 | 0 | `--` | `--` | `--` | `--` | `--` |
| cogs | 0 | 0 | 0 | `--` | 0 | `--` | 1 | `--` |
| op_income | 0 | 0 | 0 | `--` | 0 | 0 | 0 | `--` |
| capex | 0 | 0 | 0 | `--` | 0 | 2 | 0 | 0 |
| lt_debt | 0 | 1 | 0 | 1 | 2 | 1 | 0 | 0 |
| inventory | 0 | 0 | 0 | `--` | `--` | `--` | 0 | 0 |
| current_assets | 0 | 0 | 0 | `--` | 0 | `--` | 0 | 0 |
| current_liab | 0 | 0 | 0 | `--` | 0 | `--` | 0 | 0 |
| interest_exp | 0 | 0 | 0 | 0 | `--` | 0 | 1 | 0 |
| dep_amort | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 0 |
| net_income, assets, equity, cfo, cash, eps_dil, shares_dil, tax, pretax | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Only 9 of 20 concepts resolve to a single universal tag. Every other concept **requires an ordered
fallback chain**, tried in order, first hit wins, and the chosen tag recorded in
`MetricResult.raw_inputs["_tag"]` for auditability.

## 6. The finding that changes the architecture: structural N/A ≠ missing data

`JPM` (bank) has **no** `GrossProfit`, `CostOfGoods*`, `OperatingIncomeLoss`, `InventoryNet`,
`AssetsCurrent`, `LiabilitiesCurrent`, or capex tag. `SPG` (REIT) is missing most of the same set.

This is **not** a data gap to be imputed. Banks do not publish a classified balance sheet, so
"current ratio" is not a small number or an unknown number — it is **not a defined quantity**.
Inventory turnover for a bank is meaningless. Grading either one produces confident nonsense.

The design must therefore carry a **sector applicability matrix**, and metrics resolve to one of
three states, which are handled differently:

| state | meaning | weight treatment | shown in report |
|---|---|---|---|
| `OK` | computed | full weight | yes, with contribution |
| `MISSING` | applicable but data absent | weight renormalised away **and** counted against the coverage score | yes, listed under "not computed" |
| `NOT_APPLICABLE` | undefined for this business model | weight renormalised away, **no** coverage penalty | yes, listed under "N/A for sector" |

Conflating `MISSING` with `NOT_APPLICABLE` means every bank in the universe gets a low
data-coverage score and an unfairly wide confidence interval, for no reason.

### Sector classification via SIC (from `submissions.json`)

| SIC range | class | disable | require instead |
|---|---|---|---|
| 6000–6499 | Banks / financials | current & quick ratio, inventory & asset turnover, gross margin, capex intensity, FCF-based metrics, Altman Z (all variants) | NIM, efficiency ratio, tier-1 proxy, loan-loss coverage, ROA on a bank basis |
| 6500–6599, REITs | Real estate / REIT | gross margin, inventory metrics, EPS-based valuation, D/E thresholds | FFO, AFFO, P/FFO, NAV proxy, occupancy, debt/EBITDA on a REIT basis |
| 6700–6799 | Holding / investment | most operating-efficiency metrics | book-value based valuation |
| 4900–4949 | Utilities | growth-consistency penalties, high-D/E penalty (leverage is structural) | rate-base proxy, dividend coverage |
| 1311, 2911 | Energy | trailing-margin stability (commodity cyclicality) | reserve-life proxy, normalised mid-cycle margins |
| else | Industrial/commercial | — | full standard set |

`Altman Z` already ships in three variants for exactly this reason; the sector matrix chooses the
variant rather than the user guessing. **Z must never be applied to a financial** — the model was
never estimated on one.

## 7. Rate limits and caching

SEC asks for a descriptive `User-Agent` carrying a contact address and ≤10 requests/second.

- `User-Agent: Stock-Grader/<version> (<contact from config or env STOCK_GRADER_CONTACT>)`
- Token-bucket limiter at **8 rps** with jitter; a 429 backs off exponentially and is not retried
  more than 4 times.
- `companyfacts` responses are 3–7 MB — always request `Accept-Encoding: gzip`.
- Disk cache keyed by `(cik, endpoint)` with an ETag/`Last-Modified` revalidation and a default TTL
  of 24 h; `--no-cache` and `--refresh` flags. Cache the **parsed, normalised** frame, not just the
  raw JSON, or every run pays the parse cost of a 7 MB document.

## 8. Universe construction

`company_tickers.json` gives ~10k ticker⇄CIK pairs but no index membership and no market cap.
Index membership (S&P 500 etc.) is licensed data and is **not** freely available — do not pretend to
ship it. Universe options that are honest:

1. `--universe path/to/tickers.csv` — user supplies the list (documented as the recommended path).
2. `--universe all-sec` — every filer, filtered by `EntityPublicFloat` ≥ a threshold to approximate
   a liquid universe. Public float **is** in the `dei` taxonomy, so this is computable.
3. `--universe sic:3571` — every filer sharing a SIC code, which is the natural peer group for the
   sector-relative grading mode anyway.

Option 3 is the most defensible default for sector-neutral scoring: it is a real peer set derived
from data we actually have.
