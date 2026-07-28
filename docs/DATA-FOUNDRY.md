# Data Foundry — companion project design (verified 2026-07-28)

A separate project that GENERATES datasets from free sources — by archiving over
time and by computation — which the grader consumes through its existing provider
seam. Every load-bearing claim below was verified by live probes or web checks on
2026-07-28. This is a design document; nothing here starts before the grader's
revised-plan §0–§1 work is done.

## Why this works

The expensive gaps identified in Appendix A of REVISED_PLAN.md are mostly not
data — they are time and computation applied to free data:

| Bought dataset | Generated substitute | Verified status |
|---|---|---|
| Corporate-actions feed (dividends/splits) | Reconstructed from SEC XBRL companyfacts | Live-proven on 14 companies (below) |
| Analyst earnings estimates | Li-Mohanram residual-income mechanical forecasts from companyfacts | Inputs verified live incl. a small-cap bank |
| Delisted-price history (backward) | IMPOSSIBLE — archive forward instead | Confirmed: no free source exists |
| PIT universe / delisting detection | Daily git-scraped symbol directories | Endpoints live-probed |
| GICS-like classification | Hoberg-Phillips TNIC (free download, thru 2023) or local 10-K Item-1 similarity | HP site verified free |
| Earnings-calendar (historical) | 8-K Item 2.02 `acceptanceDateTime` from submissions JSON | Live-verified to the second |
| Ownership breadth | Quarterly 13F structured zips + CUSIP map from FTD files | Zips verified current (May 2026) |

## Live-probe results: corporate actions from XBRL (the keystone)

Probed 14 diverse companyfacts (JNJ, PG, AAPL, NVDA, O, JPM, AMZN, TSLA, WMT,
GE, COST, HVT, CALM):

- **Dividends per share: works.** 9 of 11 payers yield a near-complete quarterly
  series. Tags must be UNIONed (`CommonStockDividendsPerShareDeclared` +
  `...CashPaid` + `DividendsPayableAmountPerShare`) — filers switch tags
  mid-history. Fiscal Q4 is systematically missing and must be derived as
  FY − (Q1+Q2+Q3): verified exact for 17/17 fiscal years at JNJ/PG/JPM
  (e.g. JNJ 2023: 3.51 + derived 1.19 = 4.70 actual). Special dividends are
  captured (COST $5.00/2015, $10.00/2020). Small-cap fallback: aggregate
  `PaymentsOfDividendsCommonStock` ÷ shares (HVT case). REITs tag monthly (O).
- **Splits: works.** `StockholdersEquityNoteStockSplitConversionRatio1` present
  for EVERY split in sample (AAPL 7 & 4, NVDA 4 & 10, AMZN 20, TSLA 3, WMT 3,
  GE reverse tagged 0.125). Cross-check against dei share-count jumps and
  pre/post restatement ratios (AAPL FQ2-2014 appears as both 3.05 and 0.44 —
  ratio ≈ 7 confirms the split; this duplication also means split extraction is
  a HARD DEPENDENCY of dividend extraction: normalize every fact by cumulative
  split factor keyed on its `filed` date).
- **Ex-dividend dates: NOT available.** companyfacts returns numeric facts only —
  date-typed tags never appear (0/14). Consequence: total returns are buildable
  at monthly/quarterly granularity (~10–40 bps/yr timing error for a 3% yielder
  — fine for cross-sectional grading and coarse backtests), NOT daily
  benchmark-grade TR.
- **History floor:** ~2009 (XBRL mandate); later for small filers.
- Per-source adjustment-convention calibration is required before layering
  dividends onto any close series (Stooq closes are split-adjusted; dividend
  treatment undocumented; its CSV endpoint currently serves a JS challenge to
  plain clients — Tiingo is the safer close source).

## Computed signals (verified against primary literature)

1. **Mechanical earnings forecasts** — use the **Li-Mohanram (2014) residual
   income model**, NOT Hou-van-Dijk-Zhang: read from the primary source, HVZ is
   less accurate than a random walk and degrades most for analyst-uncovered
   firms; the RI model (earnings, loss dummy, interaction, book value, accruals
   = NI−CFO) is ~28–38% more accurate, needs only companyfacts, and is the
   literature's recommended input for implied cost of capital. Where analysts
   exist, consensus is still more accurate — but mechanical forecasts are
   unbiased and cover the ~half of firms with NO analyst coverage, which is
   precisely this grader's universe. Use NI−CFO accruals (banks lack
   AssetsCurrent). ~1–2 weeks.
2. **Implied cost of capital (GLS)** — composes with (1); solve price = book +
   PV(residual income) per firm (scipy brentq). Use as a cross-sectional rank,
   not absolute truth. Damodaran's implied ERP spreadsheet is free for the
   market level. ~3–5 days on top of (1).
3. **Filing-text signals** — one archiver feeds three signals: Lazy Prices
   YoY 10-K/10-Q similarity (JF 2020; expect post-publication decay; use as a
   change red flag, not alpha), Loughran-McDonald MD&A tone (dictionary free for
   academic use, paid for commercial — fine for personal use, revisit if
   productized), and local TNIC-style peer similarity from Item 1. Backfilling
   just the grader's universe is a weekend crawl at fair-access rates.
4. **8-K earnings timestamps** (`acceptanceDateTime`, verified to the second,
   distinguishes pre-open/after-close) and **13F breadth changes** (quarterly
   zips verified; CUSIP→ticker map bootstrapped free from fails-to-deliver
   files). ~1 day and ~1 week respectively.

## Architecture: two repos + one local vault

**PUBLIC `foundry` repo** (GitHub Actions; free and unlimited on public repos —
verified unchanged after the Jan 2026 pricing change):
- Daily (odd minute, e.g. `17 7 * * *`): git-scrape `company_tickers.json` /
  `company_tickers_exchange.json` / Nasdaq symbol directories; commit if changed.
  Diffs = free listing/delisting/ticker-change event stream, and the start of an
  honest PIT universe. Commit a heartbeat file EVERY run — GitHub disables
  schedules after 60 days without commits (confirmed; only commits reset it).
- Weekly: fetch `submissions.zip` (1.55 GB, one request); emit 8-K item-code
  event parquet + per-CIK sha256 vintage manifests (restatement detection).
- Quarterly: fetch `companyfacts.zip` — **NOTE: URL moved; the old bulkdata path
  returns AccessDenied; use
  `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`**
  (1.39 GB, verified) — compute corporate-action events, mechanical forecasts,
  derived datasets; publish as tagged Release assets (≤2 GiB each, unlimited
  total; Actions artifacts are unusable — 90-day retention).
- Design for cron jitter/skips (5–60+ min delays confirmed): every job is a
  watermark-based catch-up, plus a healthchecks.io ping.
- Bulk-first design also neutralizes the shared-Azure-IP rate-limit risk at SEC.
- Zero secrets needed in the public repo (all sources unauthenticated).

**PRIVATE repo or local-only vault** (Windows Task Scheduler with
run-after-missed-start + catch-up, NOT wake-timers — unreliable on laptops):
- FINRA short-interest CSVs (bi-monthly; URL pattern
  `cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv` live-verified) and
  Tiingo/Stooq price caches. **These must never enter a public repo**: FINRA
  terms prohibit end-user redistribution; Tiingo ToS prohibits redistribution
  without a paid license. Decide the repo split on day one — extracting
  restricted data from public git history later means a history rewrite.

**Connection to the grader:** a `FoundryProvider` (~150 lines per data family)
at the head of the existing `ChainedPriceProvider` (prices.py) plus a
foundry-first path in sec.py for vintage-pinned companyfacts. Contract = directory
layout + `manifest.json` per dataset: `{schema_version, source_urls,
fetched_at_utc, sha256, row_count, license_note}`. The grader refuses unknown
schema versions. Public datasets are also readable with no auth via
raw.githubusercontent.com / release URLs.

## Sequencing

MVP (daily symbol-directory scraper + heartbeat + manifest format + grader
adapter) is about a focused week and is **time-sensitive** — the PIT archive
only covers the period after it starts. Everything else ships incrementally:
corporate actions (1–2 weeks, the keystone), then mechanical forecasts, then
text signals. The one thing no computation fixes: delisted prices before the
archive began.
