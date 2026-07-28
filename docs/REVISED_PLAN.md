# Stock-Grader — Revised Implementation Plan (Fable review of Codex's plan)

Guiding rule unchanged: make every result truthful and auditable before adding features.
Second rule, added: **a sophisticated grade computed on a wrong price is worse than useless** — data-layer integrity outranks scoring refinement everywhere in this plan.

---

## 0. Step zero: stabilize the moving target — P0, before anything else

The original plan describes the last commit (f9db2a3), but most of its headline P0s already exist as uncommitted, actively-churning working-tree changes, and the churn has already broken one test.

* Stop feature work; run the full suite; fix the stale assertions in `tests/test_reporting.py` (they assert the old "letter probabilities" wording that was renamed mid-flight to "letter scenario frequencies").
* Commit the working tree in reviewable chunks. Add `.coverage` to `.gitignore` before committing.
* `pip install -e .` in CI and dev setup — two tests (`test_invariants`, `test_sec_prices` subprocess tests) currently fail only because the package isn't installed.
* Re-baseline: strike from the plan everything already implemented — unified Hazen percentile, same-curve sensitivity/letter frequencies, "model sensitivity" renaming, supervised-weighting guard, defining-pillar and weight-coverage gates, consensus N/A filtering, PIT tag selection, Tiingo CLI wiring, adjusted-close status flag, and the peers/research/valuation/backtest module set. Track only their named residuals (below).
* Process rule going forward: one section per commit, suite green before and after. Racing multiple sections concurrently is how the broken test happened.

## 1. Data integrity that silently corrupts numbers — P0

These verified bugs corrupt inputs with no warning; they outrank every scoring item.

* Dense-price freshness: no last-bar-age check exists vs `asof` — a delisted ticker's years-old close silently becomes today's price. Add the check.
* Public-float lower bound: still assigned verbatim to `snapshot.price`, feeding market cap and every multiple as if exact. Gate valuation metrics to N/A on that source, or haircut it and widen the sensitivity interval, with a per-metric flag.
* Split-basis alignment: Yahoo closes are on today's split basis, PIT DEI cover counts on the historical basis; no reconciliation. Add a price-implied vs DEI share-count consistency check (the diluted-shares scale check in `sec.py` is the template).
* `models.py` (Beneish/Ohlson/Altman) bypasses the 400-day staleness guard `fundamental.py` enforces, and `_pair` takes each concept's last two annual values independently — inputs can mix fiscal years or span multi-year gaps. Thread `asof`/`max_age_days` through, and build all model inputs from one merged annual frame aligned on the same consecutive fiscal-year pair.
* `SECInsiderPriceProvider.load` memoizes the first call's quarter set and ignores `asof` thereafter (look-ahead in any multi-date loop). Key the memo on asof.
* One shared ticker normalizer (BRK.B vs BRK-B) used by both `sec.py` and `sec_prices.py`; persist CIK, not ticker, in saved universes.
* Yahoo ranged fetches silently drop `events=div,split` and return empty beyond ~10y — fix or refuse.
* Route every sec.gov request (incl. Form-345 zips) through the single `SECClient` session: shared rate limiter, declared User-Agent, retries, circuit breaker.
* Dense-price disk cache (parquet, per provider+ticker, short TTL on the trailing bar). This — not CLI wiring — is the binding constraint: Tiingo's free tier (~50 req/h) cannot complete or repeat a full-universe run without caching. Handle 429 with backoff and a "quota exhausted, N tickers unpriced" warning.
* Cache hygiene: a `CACHE_VERSION` key embedded in cache filenames (the insider parquet stores a *derived* table whose logic changes never invalidate it today); atomic writes via temp-file + `os.replace`; stale-if-error (serve the expired copy with a warning after online retries exhaust — today it returns None with a valid stale copy on disk).
* Windows is the actual dev platform: use `%LOCALAPPDATA%` rather than `~/.cache`, and add a windows-latest CI leg.

## 2. Make the flagship grade defensible — P0

* **Unify peer selection.** The SIC-laddered peer layer exists but only the `research` command uses it; `grade`/`rank`/`consensus` still normalize banks against biotechs across the flat 82-name bundled universe. Route all commands through `select_peers` or, at minimum, normalize within SectorClass buckets with a minimum-n fallback. Note the dependency: strict sector buckets on an 82-name universe make most names ungradeable — pair this with universe expansion (§6) or use the fallback ladder.
* **Truthful "absolute" labeling.** The hybrid's "absolute" half is robust_z against the same peer cross-section; the piecewise-anchor path is unreachable by construction (MetricSpec has no anchors field, `anchors=None` hardcoded). Take the cheap honest path: rename to "peer-standardized" in every docstring, report string, and the dossier interpretation line ("an A means best-in-peer-set, not objectively strong"). Building real absolute anchors is P2-if-ever.
* **Redundancy groups.** Add `group` to MetricSpec and split each group's weight across members: the valuation pillar counts reciprocal pairs twice (P/E and earnings yield), FCF multiples three times; both Altman variants score simultaneously (`altman_variant_for` exists, is never called — wire it); ~11 overlapping trailing-return metrics share the momentum pillar. Prefer `earnings_yield` as the canonical earnings vote (well-defined for loss-makers) and retire P/E's vote.
* **Split the risk pillar** into pure risk (vol, downside dev, drawdown, VaR/CVaR, kurtosis, ATR) vs risk-adjusted return (Sharpe/Sortino/Calmar/alpha); drop or quarantine the time-series diagnostics (Hurst, variance ratio) from grading. Re-point `low_volatility`'s 0.40 weight at the pure-risk pillar it thinks it is buying.
* **Structural zeros.** Absent `dividends_paid`/buyback/R&D/inventory tags on an otherwise-complete balance sheet ⇒ value 0.0 with Coverage.OK, not penalized MISSING. Also surface pillar-composition drift: when a loss-maker's valuation pillar is computed from a different metric subset than its peers, warn — renormalization currently grades unprofitable firms on the multiples that flatter them.
* **Small-n honesty.** With the 8-peer minimum, a Hazen percentile has 12.5pp granularity and one peer decides a letter cutoff. No letter below ~15 usable peers (report a percentile range from rank binomial error instead); print n next to peer quartiles; replace quantile winsorization with fixed MAD-multiple clamps below n≈50 (at these sizes the 1%/99% winsorize is a no-op anyway).
* **Gate quality.** Gates operate at pillar granularity only — a defining pillar backed by 1 of 12 metrics passes. Require a minimum `PillarScore.coverage` in the defining-pillar/weight-coverage gates. On N/A, still render every computable pillar and the evidence table: refusal means "no composite letter", never "no information".
* **Residual cleanups from the committed fixes:** narrow the supervised-weighting guard to raise only when `forward_returns` is actually supplied (it currently hard-fails CLI `--weighting ic/shapley` even when no leakage is possible); add a first-class `frozen_weights` input for honestly pre-fitted weights; change `normalize.percentile_rank` from r/n to Hazen and delete `research.py`'s private duplicate; wire or delete `apply_hysteresis` + `previous_letters` (dead documented features are worse than absent ones); fix/delete the docstrings promising a peerless piecewise-anchor fallback that cannot fire.
* **Consensus methodology (absent from the original plan).** Averaging 11 profiles' composites averages incommensurable numbers; `clarity = 100 − 2.5·spread` is invented; the median-nearest letter can disagree with the mean score. Replace with the per-profile letter distribution (count at each letter) and drop the clarity scalar. Fix `ConsensusResult.to_dict` (embeds non-serializable dataclasses) and the "fit-weighted" docstring lie.

## 3. Evidence model: one source of truth, per-metric SEC provenance — P0/P1

* Make the pipeline `explain` payload the single evidence source and the dossier a pure renderer: `research.py` currently re-runs `build_metric_matrix` over the whole universe (doubling cost), ignores `metric_whitelist` (lists never-graded metrics as evidence), and silently mixes exact Shapley contributions with an approximate fallback that is different math under CES. Delete the fallback (mark unavailable instead), reuse the computed matrix, pass the whitelist, and add a test that dossier contributions reconcile to the composite.
* **Per-metric SEC provenance is the missing piece that matters**: record `(chosen_tag, period_end, filed, unit)` per concept at ingestion (`_select_tag` already knows and discards it) and surface it in MetricEvidence. Prioritize this over any new peer statistics.
* Fix the evidence-destroying formatting bug: unit `ratio` renders as a percentage, so asset turnover of 1.2 prints as "120.0%". Split the unit vocabulary (fraction vs multiple/x) and add a golden-file test on the markdown table.
* Bound `_trends` by asof (it currently can show fiscal periods after the dossier's stated as-of date).
* Populate `price_date`/`price_age_days` on the dense provider path (one `.index[-1]` read — today they render "—" for the common case) and show provenance (source, last bar, age, adjusted status) in the *human* report, not just JSON. When adjusted status is DERIVED_FROM_CLOSE, suppress or loudly caveat dividend-sensitive return/momentum/risk metrics.
* **Code provenance**: stamp a git SHA / methodology version in report meta next to the config and universe fingerprints — identical inputs through different code currently produce silently incomparable grades.
* Then freeze the schema. Per-peer detail lives in the comp table (§4); no evidence database.

## 4. Analyst dossier: surface the signals already on disk — P1

Days of rendering work on data already fetched; worth more than any further attribution refinement.

* **Per-peer comp table** — one row per peer with ~8 headline metrics (multiples, margins, growth, leverage) plus market cap. The numbers are already computed in the metric matrix; median/IQR-only hides which peer drags the median.
* **Share-based compensation** — `ShareBasedCompensation` is ingested and consumed by nothing, while the DCF adds SBC back and defaults dilution to zero: the tool systematically overvalues SBC-heavy names. Add `sbc_to_revenue`, an SBC trend row, default `annual_dilution_rate` to the trailing diluted-share CAGR, and show SBC-adjusted vs unadjusted FCF.
* **Insider activity** — Form 3/4/5 tables are already cached as parquet for price inference; compute trailing net insider buying/selling and transaction counts from them.
* **Company red flags** — `EdgarLabelSearch` (going concern, material weakness, restatement, bankruptcy) exists as dead code; wire it into the dossier as a per-CIK check over the trailing 2 years. Add a near-free "active 13D filer" flag. Group Beneish/accruals/Ohlson under a named "Earnings quality" subsection.
* **"What this dossier cannot tell you"** block: segment economics, guidance, consensus, backlog, customer concentration, 10-K narrative. Honesty about the free-data ceiling is an analyst feature.
* Cheap footnote checks as trend rows: receivables growth minus revenue growth; lease-adjusted leverage.

## 5. Valuation and methodology repair — P1

* **Fix the discount rate first — it is the largest unforced valuation error.** A hardcoded 10% for every firm, with bear/base/bull varying only growth. Build r = risk-free (the `RiskFreeProvider` already fetches FRED) + documented ERP (~4.5–5%), optionally scaled by shrunk beta (already computed in `statistical.py`); vary r across scenarios (±150bp); render a value/price sensitivity grid over (g, r).
* Terminal discipline: validate terminal g ≤ risk-free + ε; headline PV(terminal)/PV(total) when >75%; normalize base FCF (3-year median; flag when TTM deviates >30%); warn when scenario growth exceeds ~rf+2% with zero reinvestment; keep diluting the terminal share count (it currently freezes at year N) or state the freeze in assumptions.
* Sector gates: keep BANK/INSURANCE/REIT refusal; add UTILITY (refuse or warn — rate-base capex makes CFO−capex chronically unrepresentative), ENERGY cyclicality warning, HOLDING minority-interest note. Later frameworks (residual income/P-TB, AFFO/NAV, combined ratio) stay in the plan as-is.
* **Beneish policy (one rule, resolving the current contradiction):** never substitute neutral 1.0 for a component that *computed but fell outside the plausibility band* — that suppresses the alarm exactly when it fires. Clip to the band boundary with a raw_inputs flag, count it toward the ≤2 substitution budget; more than 2 ⇒ None.
* Average balance-sheet denominators for ROE/ROA/ROIC/CROIC/turnover/DSO/DIO/Piotroski-ROA via an `_average_latest` helper (fall back to ending value with a flag).
* `book_value_cagr_5y`: the original plan's diagnosis was wrong — it uses no share count at all (it is total-equity CAGR mislabeled per-share). Either divide by split-adjusted diluted shares per year or relabel it.
* Bank revenue: already fixed at HEAD (sector concept overrides + `_bank_revenue`). Keep only the residuals: fallback chain still degrades to gross revenue bases; post-provision NII fallback is a different quantity; `InterestIncomeExpenseNet` as an interest-expense last resort is wrong.
* Rename `raw_inputs['bankruptcy_probability']` (uncalibrated sigmoid with a frozen GNP index) to something honest, or drop it from machine output.
* `price_to_ffo` inherits `ffo_to_assets`' income-sign gate, so a REIT with a small GAAP loss but strong FFO — the case the metric exists for — gets no P/FFO. Key the gate off FFO, not income sign.

## 6. Backtesting: decide, don't drift — P2, explicitly gated

The evaluator (`backtest.py`, with its honest input attestations) is the easy half; every return claim depends on a point-in-time panel the free data layer structurally cannot feed today (no forward-return code, survivor-only universe, no delisted prices).

* **Named go/no-go decision, up front:** (a) budget ~$30–50/mo for a delisted-inclusive price source (Sharadar/Tiingo paid), or (b) adopt Shumway-style delisting-return imputation (−30% NYSE/AMEX, −55% Nasdaq) with a 0 to −100% sensitivity band, or (c) restrict all output to "surviving-universe diagnostic — no headline IC/spread numbers". Until one is chosen and a real panel passes all five attestations, every section-8 deliverable is labeled "evaluator only — no return results possible".
* If pursued, the panel program in order: EDGAR filer-index historical universe keyed by CIK (10-K/10-Q filed in trailing 12–18 months as of signal date; exits via Form 25/15) — this also fixes cross-section size for §2's sector-relative grading; forward *total* returns from a verified-adjusted source; immutable content-addressed companyfacts snapshots (the 24h-TTL cache destroys the vintage a panel was built from); label-aware purging (drop train rows whose return window overlaps test, embargo defaulted from horizon — the current splitter purges nothing); a fold-fitting harness (fit weights on train, freeze, score test) so supervised weighting has an honest path; blocked bootstrap with block ≥ horizon overlap; cost ladder (0/25/50/100/200bp) instead of one number; long-only variant (the bottom-quantile short is not implementable in the names this tool ranks); power floor (≥100 names, ≥36 periods, else an `underpowered` flag); pre-registered primary spec + Benjamini-Hochberg across the grid + trial count in the report.
* Cheap fixes now regardless of the decision: `validate_distress.py` snapshots Dec-31 of the label year (the metrics read the same financials that triggered the going-concern opinion — discrimination, not prediction) with 30 hand-picked mega-cap controls (size confound). Move asof 6–12 months before the label filing, size-match controls, fix the fabricated `filed_date` fallback, and relabel it a discrimination check.

## 7. Engineering trust bundle — P0 (about a day, highest trust-per-hour)

* Golden numeric tests: hand-computed Beneish/Ohlson/Altman values (a transposed coefficient currently passes CI) plus a handful of plain ratios.
* `--cov-fail-under` at the current level; drop mypy's `|| true` for already-clean modules; build the wheel, `twine check`, and run tests against the installed wheel; pip-audit; a lockfile/constraints file (grades depend on pandas/numpy rank and RNG behavior that drifts across versions — CI installs unpinned across 3.11–3.13).
* Real end-to-end CLI tests on fixture data, not wholesale monkeypatching: graded target; N/A target (assert exit code 3 *and* that evidence still prints); bank valuation-refusal; `CIK:` identifier parsing.
* Security hardening: size caps and path-traversal guards on Form-345 zip extraction; download size limits per provider; keep `TIINGO_API_KEY` out of logs, errors, and provenance meta.
* Shapley factorial pre-check (`math.factorial(p)` before materializing — p=10 currently builds 3.6M tuples to sample 200) whenever `weighting.py` is next touched.

## 8. Compliance, licensing, PIT semantics — P1 (new section; cheap and load-bearing)

* Disclaimer coverage: "research screen, not investment advice" exists only in the research dossier. Put it on every renderer — grade/rank/consensus terminal, markdown, and JSON meta. A–F letters read as recommendations.
* Data-terms audit before any sharing/export feature: Tiingo redistribution limits on prices embedded in dossiers; Yahoo's unofficial-API ToS position (keep last in chain, never default); StockAnalysis opt-in only; FRED citation; provenance of the bundled universe list.
* PIT timing semantics: `filed <= asof` compares dates only — filings accepted after 5:30pm ET disseminate the next business day, so date-granularity PIT admits same-day look-ahead. Define the asof timezone convention and either use acceptance datetimes or document the bias.

## 9. Deferred — P2+

User workflows (compare/screen/watchlists/alerts) as originally planned; absolute anchors; bank/REIT/insurer valuation frameworks; 13F ownership; performance work (consensus re-runs grade_universe 11×; Ledoit-Wolf Python loop).

**Explicitly out of scope (stop doing):** new weighting methods or aggregators (cut the user-facing registry to fixed/equal, rest behind a research flag), further attribution/interval precision (the current exactness already outruns input quality by orders of magnitude), per-peer evidence databases, extension-taxonomy ingestion.

---

## Appendix A — Free-data roadmap (added 2026-07-28, terms verified July 2026)

Sequenced by value-per-effort. Nothing here starts before §0-§1 are done. Tier 0 and
Tier 1 items are hours-scale each and slot into §4; Tier 2 items are the first
genuinely new acquisitions.

**Tier 0 — data already on disk, unused (do first; zero acquisition, zero new ToS):**
1. Insider transaction features from the cached form345 parquet (net buying,
   cluster buys, officer-vs-director, transaction codes).
2. Wire the dead EFTS red-flag client (going concern / material weakness /
   restatement) into the dossier.
3. Use ingested-but-unconsumed `share_based_comp`: SBC/revenue metric, SBC trend
   row, SBC-adjusted FCF, dilution default from diluted-share CAGR.

**Tier 1 — near-free wins (hours to ~1 day each):**
4. 8-K item codes from the already-cached submissions JSON (verified live: `items`
   field): flag 4.02 non-reliance, 4.01 auditor change, 3.01 delisting notice,
   1.03 bankruptcy, 2.04 debt acceleration, 5.02 executive departures. Plus
   Forms 25/25-NSE/15 (delisting/deregistration) from the same JSON. Zero new requests.
5. Fama-French FF49/FF12 SIC-range industry mapping (free static file, vendor it) as
   the peer-group key — replaces raw 4→3→2-digit SIC fallback; the single biggest
   peer-quality lever available.
6. Swap `company_tickers.json` → `company_tickers_exchange.json` (adds exchange;
   filter OTC junk out of peer universes). Under an hour.
7. Persist Tiingo's divCash/splitFactor columns (currently discarded): shareholder
   yield input + free split-validation against SEC share counts.
8. Treasury.gov yield-curve XML as primary/fallback beside FRED; add FRED DBAA−DAAA
   credit spread and CPI (deflate 5y growth trends). CAUTION: FRED's ICE BofA
   (BAML*) series are truncated to a rolling 3-year window since April 2026 — use
   Moody's DBAA/DAAA, not BAML*, for anything historical.
9. Damodaran (NYU Stern) free datasets: industry ERP, betas, margins, WACC, EV
   multiples — feeds the §5 discount-rate build and dossier multiple sanity checks.
10. SEC AAER / enforcement-release flag via the generalized EFTS helper — the
    strongest free quality-pillar red flag; one more query in the same event module.

**Tier 2 — new sources that clear the bar (days each; after Tiers 0-1):**
11. FINRA bi-monthly Equity Short Interest CSVs (free, archives to 2014, PIT by
    construction): short-interest ratio, days-to-cover, change — best genuinely new
    signal found. Non-commercial use only; do not redistribute the data.
12. XBRL frames API (one call = one concept for every filer) for market-wide
    percentile context. Prefer this over the 1.5 GB bulk companyfacts.zip until a
    universe-scale ambition actually exists. Caveats: no dimensions, last-filed-wins.
13. Delisting reference + PIT universe down-payment: start archiving daily Nasdaq
    Trader symbol-directory snapshots NOW (free, keyless); Alpha Vantage
    LISTING_STATUS `state=delisted` (confirmed free with registered key) to label
    historical dropouts.
14. 13F quarterly flattened zips (institutional ownership breadth/change for the
    dossier) + fails-to-deliver files (liquidity red flag, and the FTD file's
    CUSIP+ticker pairs bootstrap the CUSIP map that 13F needs).
15. FSNDS (Financial Statement AND Notes data sets): the only free source of
    segment-level facts (monthly TSVs carry a `segments` field). The biggest
    dossier unlock (segment revenue/margin for multi-segment names) but real TSV
    plumbing; archive monthlies (they consolidate to quarterlies after a year).
16. Post-Dec-2024 SC 13D/G structured XML: activist-stake event flags (discovery
    via EFTS; don't parse the pre-2024 HTML backlog).

**Skip (verified reasons):** Cboe anything (ToS prohibits programmatic extraction);
Google Trends (pytrends archived 2025; unofficial endpoints violate ToS); job
postings/web traffic (no legitimate free path); USPTO patents (entity-resolution
cost, sector-degenerate signal); GLEIF/Wikidata (duplicate EDGAR with worse
provenance); BLS/BEA (cancels out of cross-sectional grades); DEF 14A
pay-vs-performance iXBRL (probed: no ecd facts in companyfacts — needs a full
iXBRL parser; closed until governance becomes a priority); Finnhub recommendation
trends (only free consensus-shaped signal, but read its ToS first — data-deletion
clause conflicts with our caching; first keyed vendor dependency).

**Hard wall, stated honestly:** no free source provides delisted-stock price
history. A survivorship-free backtest is not buildable for $0 — archive PIT
universes from today forward, label dropouts with the free lists above, disclose
the bias for earlier periods, or budget ~$20-80/mo (EODHD/Sharadar) at §6's gate.

**ToS watch on the existing stack:** the shipped stockanalysis.com provider is an
unofficial internal API in the same gray class as the Yahoo chart endpoint — keep
both opt-in/last-resort, never default. Tiingo free tier (verified: 50 req/h,
1,000/day, 500 unique symbols/month, personal use only) caps price-dependent
pillars at peer-group scale — universe-wide percentiles are feasible for
fundamentals-only pillars, and reports must not redistribute vendor price data.
