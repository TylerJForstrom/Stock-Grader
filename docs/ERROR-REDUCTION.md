# Error Reduction — Prioritized Action Plan

Status: chief-engineer synthesis, 2026-07-24. Supersedes the eight individual research memos.
Authority on data sources remains `docs/design/DATA-GROUND-TRUTH.md`.

The defect class this plan targets is the one this project keeps finding: **a confident wrong
number, not an exception**. Every one of the seven bugs already fixed (Q4 derivation, `fy`/`fp`
misuse, negative share counts, non-contiguous `ttm()`, inverted momentum direction, flat-50
normalizers, out-of-band confidence intervals) produced a plausible value that nothing rejected.
Items are ranked accordingly: a change that converts a wrong number into `MISSING` outranks a
change that makes an already-honest number more precise.

---

## How this list was filtered

101 audited findings came in across 8 research areas.

| Disposition | Count | Where they went |
|---|---|---|
| **Dropped — `WRONG`** | 6 | Deleted. The load-bearing claim did not survive re-measurement. |
| **Dropped — `ALREADY_DONE`** | 4 | Deleted, except where a genuine residual survived (called out below). |
| `OVERKILL` | 8 | Moved to **Deliberately not doing**, with reasons. |
| `VALID` | 83 | Merged down to **41 actionable items**. |

**The 6 dropped as WRONG**, so nobody re-proposes them:

1. *"A single price per ticker restores 16 metrics"* — measured: zero `needs_prices` metrics have
   `min_history == 0`, so the proposed message would print "restores 0". The premise (valuation
   metrics are `needs_prices`) is false; they route through `market_cap` and go `MISSING` via
   `safe_div`.
2. *"Peer-relative magnitude bounds via SEC frames catches the MCD share-count error"* — measured
   frame `q001` for `WeightedAverageNumberOfDilutedSharesOutstanding` is **57 shares**, not ~1e6,
   so the proposed band `[q001/10, q999*10]` contains the corrupt value 751.8 comfortably. The
   panel is polluted by other filers making the identical unit error.
3. *"Dirichlet concentration should be calibrated from a weight bootstrap"* — the bootstrapped
   quantity (metric-level weights) never reaches `uncertainty_interval`, which only ever sees
   **pillar** weights, and every shipped profile supplies `pillar_weights` so the vector has no
   sampling distribution at all. (A defensible one-line residue survives — see DO NEXT W2.)
4. *"Sector classification is ambiguous; build a two-point mixture model"* — the proposed
   `_AMBIGUOUS` table is mostly no-ops against the real `_SIC_RANGES`: 6199 already maps to BANK,
   6200-6299 to HOLDING, and 6798 *and* 6500-6599 both already map to REIT.
5. *"No cross-sectional winsorization; dispersion weighting reads raw column moments"* — false.
   `pipeline.py:227` passes `scores[members]`, the **post-normalisation 0-100 matrix**. A raw
   outlier of 28.34 never reaches `entropy`/`critic`/`pca`/`_covariance`. (The salvageable half —
   declaring `winsor` on the 43 unbounded metrics — is kept.)
6. *"Build the fallback chains empirically from SEC DERA data sets"* — its flagship claim, that
   `InterestExpenseNonoperating` "is not in the chain at all", is contradicted by
   `concepts.py:69`, where it sits at position 2 with a comment recording the exact mining run the
   finding proposes repeating. Visa's frozen interest expense is a per-period-resolution defect,
   not a missing tag.

**The 4 dropped as ALREADY_DONE:** Form 4 non-derivative transactions as a price source and SEC
bulk insider ZIPs (both shipped in `data/sec_prices.py`, and via the bulk quarterly TSV rather
than per-accession XML, which is strictly better); the Chapter 11 outcome-label harness and the
restatement / going-concern labels (both shipped in `validation.py` +
`scripts/validate_distress.py` — that harness has already earned its keep by catching the
`accruals_ratio` inversion). Two residuals survived and are carried forward: making the distress
harness an offline fixture-backed pytest, and reading `SECURITY_TITLE` / `FILING_DATE` in the
insider loader.

### Duplicates merged

Several areas independently found the same thing. The big merges:

- **Tag-chain history** — "resolve per period" + "splice with an overlap gate" + "guard the splice
  with equivalence classes" are one change (DO NEXT W1.1).
- **`liabilities` is missing for 12.5% of filers** — found twice, with a useful disagreement about
  the fix that resolves cleanly (DO NOW 4).
- **Median imputation before estimating association** — found three times (covariance geometry,
  CRITIC redundancy, supervised IC attenuation); one root cause, two fixes (DO NEXT W3).
- **The confidence interval is not 90%** — the "halve the width" defect and the "report letter
  probabilities" feature are the *same* change to the same loop (DO NOW 9).
- **Survivorship** — ticker→CIK filtering, ticker *reuse*, and the bundled universe are one guard
  plus one new entry point (DO NOW 12).
- **Sparse columns capture dispersion weighting** — found twice, same patch (DO NEXT W3.1).
- **SEC frames as an oracle** — proposed three times for three different purposes; one client,
  three consumers (DO NEXT W7.2).

---

## Facts I re-verified before ranking

Measured directly against this checkout, because several audits disagreed:

| Claim | Verdict |
|---|---|
| 105 metrics registered | **Confirmed.** risk 24, valuation 16, momentum 13, health 12, profitability 11, growth 9, quality 6, efficiency 6, shareholder 5, liquidity 3. |
| `needs_prices` metric count | **40**, not 56. Valuation metrics are *not* `needs_prices`. |
| Metrics with `winsor=None` | **43** of 105. |
| Test count | **246 collected**, not 220. The brief is stale. |
| Default universe size | **82 unique tickers** across 32 lines. Two audits said "~24 names" by counting lines; they were wrong, and it matters — SE(percentile) at p=0.5 is **5.5 points at N=82**, not 10.2, so the peer-sampling contribution to a hybrid headline is ±2.8, not ±5.1. |
| `apply_hysteresis` call sites in `src/` | **Zero.** Defined, exported, named in a signature, never invoked. |
| `sec.py:360` first-present-wins resolution | Confirmed verbatim. |
| `sec.py:365-369` instant series written into **both** quarterly and annual frames | Confirmed verbatim. |
| `types.py:172-186` — `latest()` has no age bound, `history()` has no span check | Confirmed verbatim. |
| `fundamental.py:42-43` `debt = ... or 0.0`, `cash = ... or 0.0` | Confirmed verbatim. |
| `statistical.py:54-56` returns `pd.Series(0.0)` when `risk_free` is None | Confirmed verbatim — and contradicts its own module docstring at lines 9-11. |
| `sec.py:195-201` `_usd_records` falls through to `next(iter(units.values()))` | Confirmed verbatim. |
| `concepts.py:62` `...BeforeIncomeTaxesDomestic` in the `pretax_income` chain | Confirmed. |
| `concepts.py:95` `"liabilities": ("Liabilities",)` — no fallback | Confirmed. |
| `synthetic.py:68` `seed = abs(hash(ticker))` under a docstring promising determinism | Confirmed. |

---

# DO NOW

Twelve items. Every one fixes a **currently-live wrong number** and every one is trivial or small.
Total estimated effort: 3-5 focused days. Ordered by impact ÷ effort.

---

### 1. Build a real annual frame for instant concepts, and make `history()` span-aware

**Impact: critical · Effort: small · Blast radius: every company, every growth metric**

`sec.py:365-369` assigns the *same* instant series to `quarterly[concept]` and `annual[concept]`.
`history()` then takes the last *n* rows with no span check, so a "5-year" CAGR is computed from
six consecutive **quarters**.

- Measured: `CAT.history('equity', 6, annual=True)` returns
  `[2024-12-31, 2025-03-31, 2025-06-30, 2025-09-30, 2025-12-31, 2026-03-31]` →
  `book_value_cagr_5y = -0.0087`. AAPL, same shape → `+0.09790`.
- Measured: `CAT.history('net_income', 6, annual=True)` returns
  `[2010-12-31, 2021, 2022, 2023, 2024, 2025]` → `earnings_cagr_5y = 0.26896`, a **15-year span
  labelled 5 years**.

**Files:** `src/stock_grader/data/sec.py`, `src/stock_grader/types.py`,
`src/stock_grader/metrics/fundamental.py`

**Sketch:**

```python
# sec.py — stop writing instants into the annual frame; resample at fiscal year-ends.
if PERIOD_TYPES[concept].value == "instant":
    series = normalize_instant_facts(fact, pit_mode=pit_mode, asof=asof)
    if not series.empty:
        quarterly[concept] = series
        # anchor on ONE reliable duration concept's annual index (revenue, else net_income),
        # or submissions.json fiscalYearEnd — NOT the union of every duration concept,
        # which picks up off-cycle entries.
        annual[concept] = _at_fiscal_year_ends(series, fy_ends, tol_days=45)

def _at_fiscal_year_ends(s, fy_ends, tol_days=45):
    idx = pd.to_datetime(pd.Series(s.index))          # index may be datetime.date
    keep = {}
    for e in fy_ends:
        d = (idx - pd.Timestamp(e)).abs()
        if d.min() <= pd.Timedelta(days=tol_days):
            keep[e] = s.iloc[int(d.idxmin())]
    return pd.Series(keep).sort_index()

# types.py — history() refuses a window whose spacing is not what the caller asked for.
def history(self, concept, n, *, annual=True, max_gap_years=1.6):
    ...
    series = frame[concept].dropna().iloc[-n:]
    if len(series) < n:
        return None
    step = pd.Series(pd.to_datetime(series.index)).diff().dropna().dt.days
    if annual and (step > 365 * max_gap_years).any():
        return None
    return series

# fundamental.py:280 — years must be elapsed time, not row count.
years = (pd.Timestamp(history.index[-1]) - pd.Timestamp(history.index[0])).days / 365.25
if not (target * 0.8 <= years <= target * 1.25):
    return None
cagr(history.iloc[0], history.iloc[-1], years)
```

**Verify:** assert `CAT.history('equity', 6, annual=True).index` spans ≥ 4.5 years or returns
`None`; assert `book_value_cagr_5y` and `earnings_cagr_5y` are either `None` or computed over
4.0-6.25 elapsed years for all 82 default-universe names; add a golden test that pins the
post-fix AAPL value. Expect growth-pillar coverage to **drop** — that is the fix working.

---

### 2. Put an age contract on `latest()`, and stop treating unknown debt as zero debt

**Impact: high · Effort: trivial · This is the cheap insurance that de-urgents item W1.1**

`types.py:172-178` returns the last non-NaN value regardless of age and never sees
`snapshot.asof`. `fundamental.py:42-43` then converts "unknown" into "none":

```python
debt = _latest(snapshot, "total_debt") or 0.0
cash = _latest(snapshot, "cash") or 0.0
```

Measured consequence: Lowe's `long_term_debt` resolves to `LongTermDebtNoncurrent`, whose series
**ends 2009-10-30 at $4.524B**, while `LongTermDebtAndCapitalLeaseObligations` (position 3 in the
same chain) runs to 2026-05-01 at $36.751B. Result: `debt_to_assets = 0.0924` against a true
~0.68, and `net_debt_to_ebitda ≈ 0.31x` against ~2.90x. Lowe's currently grades as effectively
debt-free.

**Files:** `src/stock_grader/types.py`, `src/stock_grader/metrics/fundamental.py`

**Sketch:**

```python
# types.py
def latest(self, concept, *, annual=False, asof=None, max_age_days=None):
    ...
    series = frame[concept].dropna()
    if series.empty:
        return None
    if asof is not None and max_age_days is not None:
        age = (pd.Timestamp(asof) - pd.Timestamp(series.index[-1])).days
        if age > max_age_days:
            return None
    return float(series.iloc[-1])

# fundamental.py
def _latest(s, concept, **kw):
    return s.fundamentals.latest(concept, asof=s.asof,
                                 max_age_days=s.fundamentals.max_age_days, **kw)

def _enterprise_value(snapshot):
    cap = snapshot.market_cap
    debt = _latest(snapshot, "total_debt")
    if cap is None or debt is None:
        return None                       # unknown debt is unknown, not zero
    cash = _latest(snapshot, "cash") or 0.0
    ev = cap + debt - cash
    return ev if ev > 0 else None
```

**Default the bound to ~400 days, not the 200 originally proposed** — a 10-K-only filer's balance
sheet is legitimately up to ~15 months old, and 200 would mass-delete them. Make it a
`GradeConfig` value. Append a snapshot warning naming every concept past the bound.

**Verify:** `debt_to_assets` for LOW must become `None` (not 0.0924) until W1.1 lands, then ~0.68
after. Assert `_enterprise_value` returns `None` when `total_debt` is absent. Watch
`MIN_COVERAGE_TO_GRADE = 0.35` — measure how many of the 82 names fall below it before merging.

---

### 3. Delete the domestic-only pretax tag from the fallback chain

**Impact: high · Effort: trivial (one line) · Highest impact-per-character in the plan**

`concepts.py:62` lists `IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic` in the
`pretax_income` chain. A US-domestic-only subtotal is **never** a valid synonym for consolidated
pretax income.

McDonald's resolves to it today: FY2024 pretax `3.282e9` against net income `8.223e9`; median
relative error 0.956 over 12 annual periods, failing **12 of 12**. `sec.py:418-419` then builds
`ebit = pretax_income + interest_expense` on a number ~3x too small, corrupting EBIT margin,
interest coverage, EV/EBIT and EBITDA.

**File:** `src/stock_grader/data/concepts.py`

**Sketch:** delete line 62. Then add the identity as a standing check in `build_fundamentals`,
evaluated on the annual frame:

```
|pretax - tax - minority_interest - net_income| / max(|net_income|, scale) <= 0.15   → ok
                                                                     <= 0.40   → warn
                                                                      > 0.40   → demote the chain entry, mark the period suspect
```

Measured false-positive rate of that identity across the other nine sampled filers: **1 in ~90
firm-years** (NVDA/TGT/AAPL/JPM/JNJ all 0.0000; ABBV 0.0003; BLK 0.0083; WMT 0.0215; XOM 0.0324).

**Verify:** MCD `pretax_income` FY2024 must resolve to ≈ `1.06e10`, not `3.282e9`; the identity
must pass at 15% for ≥ 11 of 12 MCD years post-fix; assert no default-universe name fails the
identity at 40%.

---

### 4. Derive `liabilities` when the tag is absent — but **not** from `LiabilitiesAndStockholdersEquity` directly

**Impact: critical · Effort: trivial**

`concepts.py:95` is `"liabilities": ("Liabilities",)` — a chain of one, with no derivation in
`_derive`. Measured: **827 of 6,628** `Assets` filers (12.48%) never tag it. Walmart, Nike and TJX
omit it from their entire `companyfacts` payload. Both `ohlson_o_score` and `altman_z_prime`
return `None` for all three; the grade is still issued, silently missing its solvency input.

Two audits proposed conflicting fixes. **They resolve cleanly, and the resolution matters:**

- ✅ Derive `liabilities = assets − equity_including_NCI`. Verified: reproduces the reported
  `Liabilities` to a **median relative error of 0.000000** for filers that report both, and
  recovers 69 / 72 / 31 / 13 periods for MCD / WMT / ABBV / TGT.
- ❌ **Do NOT add `LiabilitiesAndStockholdersEquity` to the `liabilities` chain.** Measured: that
  tag equals total `Assets` for **6,546 of 6,584 filers (99.42%)**, median ratio to true
  `Liabilities` 1.911. Putting it in the chain sets `liabilities = assets` for exactly the 827
  filers being repaired, forcing TL/TA to 1.0 and tripping Ohlson's `OENEG` indicator — a new
  confident wrong number introduced by the repair.

**Files:** `src/stock_grader/data/sec.py` (`_derive`), `src/stock_grader/data/concepts.py`

**Sketch:**

```python
# concepts.py — new chains
"liab_and_equity": ("LiabilitiesAndStockholdersEquity",),
"redeemable_nci": ("RedeemableNoncontrollingInterestEquityCarryingAmount",),

# sec.py::_derive — filed tag always wins; this only fills absence.
if "liabilities" not in df and "equity" in df:
    base = df["assets"] if "assets" in df else df.get("liab_and_equity")
    if base is not None:
        equity_total = df["equity"] + df.get("minority_interest", 0.0).fillna(0.0)
        df["liabilities"] = base - equity_total
```

While here, fix the related defect in the same function: `invested_capital` (`sec.py:433-437`)
mixes **parent-only** equity with **consolidated** debt, overstating ROIC for any filer with NCI.
`concepts.py:79` already carries
`StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`, so this is a
re-prioritisation, not a new tag. Keep parent-only equity for ROE and book value per share.

**Verify:** assert WMT (CIK 104169), NKE and TJX yield non-`None` `liabilities`, `ohlson_o_score`
and `altman_z_prime`. For the ~5,801 filers that report both, assert median
`|derived − reported| / reported < 1e-6`. Expect ~750 of 827 recovered (the two audits said 750
and 788 — I could not reconcile that offline; measure it).

---

### 5. Detect the reporting currency and refuse to mix units

**Impact: critical · Effort: small (scoped to the load-bearing 90%)**

`_usd_records` (`sec.py:195-201`) tries `USD`, `USD/shares`, `shares`, `pure`, then **falls
through to whatever unit happens to be first**, and discards the key. `types.py:109` defaults
`currency = "USD"` and `build_fundamentals` never sets it. `sec.py:410-413` then literally adds
JPY to USD inside `_derive`.

Measured live, not hypothetically:

- Toyota (CIK 0001094517): unit histogram **JPY 423 / USD 316**. Running the real engine gives
  `debt_to_assets = 28.342` and `debt_to_equity = 20.0` (saturated at `safe_div`'s cap), because
  JPY long-term debt of 1.07e13 (2020) is divided by a USD convenience-translated asset base of
  3.77e11 that **stops in 2013**.
- ASML (CIK 0000937966): **EUR 548 / USD 3** us-gaap tags, all loaded as USD.

Prices are live via `data/sec_prices.py`, so the valuation-metric contamination path is real.

**Files:** `src/stock_grader/data/sec.py`, `types.py`, `metrics/engine.py`, `tests/test_sec_parsing.py`

**Sketch:**

```python
def _unit_records(fact):
    units = fact.get("units", {})
    for key in ("USD", "USD/shares", "shares", "pure"):
        if key in units:
            return key, list(units[key])
    k, v = next(iter(units.items()), (None, []))
    return k, list(v)

# build_fundamentals: collect resolved units, take the modal MONETARY unit,
# drop any concept whose monetary unit differs from it (-> Coverage.MISSING),
# warn naming the dropped concepts, and set Fundamentals.currency.
MONETARY = lambda u: u and u not in ("shares", "pure") and "/" not in u
```

Gate market-cap-consuming metrics to `MISSING` when `currency != "USD"` — price and shares come
from outside the XBRL payload and are USD by construction.

**Skip** the "prefer the unit with the longest and most recent record span" tiebreak that was
proposed alongside this. It changes which series loads for *every* dual-reporting filer to solve a
smaller problem.

**Verify:** check in the Toyota and ASML `companyfacts` payloads as fixtures; assert
`build_fundamentals(TOYOTA).currency == "JPY"` and `debt_to_assets is None`; assert
`build_fundamentals(ASML).currency == "EUR"`; assert an all-USD filer is byte-identical to today.

---

### 6. Gate the derived Q4, and delete the two `.abs()` calls that hide the failure

**Impact: critical · Effort: small**

`sec.py:289-296` derives Q4 as FY − Q1 − Q2 − Q3, after `sec.py:238-242` has independently
`_select`-ed each period — so the FY value can come from a **different restatement vintage** than
the quarters subtracted from it.

Measured: Target FY2013 capex was filed at `3,453,000,000` (2014-03-14) and restated to
`1,886,000,000` (2015-03-13, 2016-03-11), while YTD-Q3 stayed at `2,839,000,000` in **both**
vintages. Derived Q4 capex = **−953,000,000** — the only negative in the whole series — and
`sec.py:424` plus `types.py:139` call `.abs()` on it, silently converting it into a plausible
+953M outflow inside FCF.

**Do NOT ship the filed-date vintage guard** that was proposed with this. It does not fire on its
own motivating example: Target's winning FY record was filed 2015-03-13 / 2016-03-11, *after* all
four quarters (2013-05-30 … 2014-11-26). Use the value-conflict test instead (3453 vs 1886 = 45%,
fires correctly), which makes DO NEXT W1.5 a prerequisite for the strong form.

**Files:** `src/stock_grader/data/sec.py`, `src/stock_grader/types.py`

**Sketch:**

```python
# sec.py, in the Q4 derivation
SIGN_CONSTRAINED = {"revenue", "cogs", "capex", "depreciation_amortization",
                    "shares_basic", "shares_diluted", "buybacks", "dividends_paid"}

q4 = fy_value - sum(q1, q2, q3)
sibs = [q1, q2, q3]
med  = float(np.median(np.abs(sibs)))
if concept in SIGN_CONSTRAINED and q4 < 0:
    warn(f"{concept} {period}: derived Q4 is negative ({q4:,.0f}); dropping")
elif med > 0 and not (0.15 * med <= abs(q4) <= 6.0 * med):
    warn(f"{concept} {period}: derived Q4 magnitude {q4:,.0f} outside 0.15-6.0x sibling median")
else:
    emit(q4)

# then DELETE the masks
- df["fcf"] = df["cfo"] - df["capex"].abs()
+ df["fcf"] = df["cfo"] - df["capex"]        # capex is filed as a positive outflow
# and the matching .abs() in types.py:139
```

Removing the `.abs()` calls is **necessary, not optional** — otherwise the new check has nothing
left to catch, because the mask has already made the value plausible.

**Verify:** assert TGT's quarterly `capex` series contains **no negative values**; assert the
2014-02-01 period is absent with a warning rather than present at +953M; assert TGT FY2013 `fcf`
changes by ~1.9B; assert no default-universe name loses more than 2 derived quarters.

---

### 7. Stop using `adj_close` as the market-cap price

**Impact: critical · Effort: trivial**

`cli.py:139` sets `snapshot.price = float(frame["adj_close"].dropna().iloc[-1])`, and
`types.py:216-219` computes `market_cap = price * shares_outstanding`, consumed by 16 valuation
metrics. Under `--asof`, the last bar's adjusted close sits **below** its raw close by the entire
cumulative dividend adjustment.

Measured on real bars for 2018-07-25: **T** `c=30.25 / a=14.094` (−53.4%); **SPY** `c=284.01 /
a=251.28`; **BRK.B** `c=197.61 / a=197.61` (0.0% — the control that proves the mechanism).

Every backtested multiple comes out deflated in near-exact proportion to dividend yield, so a
valuation backtest would "discover" that dividend yield predicts cheapness. **This is live today,
not latent** — `CSVPriceProvider` works offline, so any `--price-dir X --asof 2018-07-25` run
already produces it.

**Files:** `src/stock_grader/cli.py`, `src/stock_grader/types.py`, `tests/test_invariants.py`

**Sketch:**

```python
# cli.py — market cap wants the traded price; statistical.py::_prices() keeps adj_close.
col = "close" if frame["close"].notna().any() else "adj_close"
snapshot.price = float(frame[col].dropna().iloc[-1])
if col == "adj_close":
    snapshot.meta["price_is_adjusted"] = True
    snapshot.warnings.append(
        "no raw close column; market cap uses adjusted close and is understated "
        "by cumulative dividends")
```

**Verify:** build a synthetic series with a known 4%/yr dividend adjustment; assert
`market_cap(asof=t)` tracks `close(t) * shares` to within 0.1% **for every t**, rather than
decaying as `t` recedes. Re-run the BRK.B control: its market cap must be unchanged by this fix.

---

### 8. Make `--asof` honest: raise under LATEST, and truncate the share-count series

**Impact: critical · Effort: small**

Two paths let a date be accepted and then ignored.

**(a)** `SECProvider.fetch` defaults `pit_mode=PitMode.LATEST` (`sec.py:461`), and `_select`
filters on `filed <= asof` **only** under `PIT` (`sec.py:178`). So `asof` is accepted and
discarded. Measured: BBBY at `asof=2019-01-01` returns assets of **7,319,863,000** with a last
period of 2018-09-01 under PIT, versus **2,225,217,000** and 2023-02-25 under the default — four
years of leakage from one omitted keyword. A distress model scored on post-bankruptcy balance
sheets looks superb.

**(b)** `sec.py:498-499` takes `snap.shares_outstanding = float(series.iloc[-1])` from the DEI
series with **no `asof` filter in either mode** (`normalize_instant_facts` keys on the fact's own
end date). So `--asof 2020-01-01` without `--pit` computes `market_cap = price(2020) *
shares(2026)` — silent look-ahead in the denominator of every valuation metric, the same class as
the already-fixed negative-share-count bug. Same exposure on the `shares_diluted` fallback at
`sec.py:509-514`.

**File:** `src/stock_grader/data/sec.py`

**Sketch:**

```python
# (a) in fetch()
if asof is not None and asof != date.today() and pit_mode is not PitMode.PIT:
    raise ValueError(
        f"asof={asof} is ignored under PitMode.LATEST — pass pit_mode=PitMode.PIT "
        f"for point-in-time selection, or drop asof")

# (b) at sec.py:498 and again at the shares_diluted fallback
if asof is not None:
    series = series[series.index <= asof]
    if series.empty:
        snap.warnings.append(f"no {concept} observation on or before {asof}")
        continue
snap.shares_outstanding = float(series.iloc[-1])
snap.meta["shares_date"] = series.index[-1]
```

Skip the post-condition assertion proposed alongside (a): `Fundamentals.filed` is populated only
from **duration** facts (`sec.py:378-379`), so instants would be uncovered and the assertion would
give false assurance.

**Verify:** assert `fetch(cik, asof=date(2019,1,1))` raises; assert
`fetch(cik, asof=date(2019,1,1), pit_mode=PitMode.PIT).fundamentals.latest('assets')` ==
7,319,863,000 for BBBY; assert `meta['shares_date'] <= asof` for every snapshot in a
`--asof 2020-01-01 --pit` run.

---

### 9. Fix the confidence interval, and get letter probabilities for free

**Impact: critical · Effort: small · The printed "90% CI" is currently false**

`report.py:60` prints "90% CI". `pipeline.py:159-183` `_curve_interval` maps the raw interval
affinely — `w*low + (1-w)*pct` — adding the **same constant** to both ends, which (a) multiplies
the half-width by `absolute_weight` (0.5 by default) and (b) assigns the percentile **zero
width**, even though the percentile is a function of the same resampled composite.

Measured coverage against the advertised 90%, as 10/20/30/40% of pillars are masked:
**0.697 / 0.551 / 0.439 / 0.395**. Median hybrid half-width 2.16 versus raw 4.32 — exactly halved.
Meanwhile the percentile's *own* 90% span under the existing draws, with peers held fixed, has a
median of **22.5 points** and a p90 of 34.7, all of it currently discarded.

The fix and the letter-distribution feature are the **same loop**.

**Files:** `src/stock_grader/scoring.py`, `pipeline.py`, `types.py`, `report.py`

**Sketch:**

```python
# scoring.py — also return the draws.
return IntervalResult(low, high, samples=samples)

# pipeline.py — rank each draw against the FIXED peer composites; delete _curve_interval.
peers = np.sort(peer_composites)                     # computed once
pct   = np.searchsorted(peers, samples, side="left") / len(peers) * 100.0
blend = absolute_weight * samples + (1 - absolute_weight) * pct
low, high = np.percentile(blend, [5, 95])
grade_probs = pd.Series(Counter(to_letter(b) for b in blend)) / len(blend)
```

This needs **no new parameter, no peer bootstrap and no guessed correlation** — unlike the
`rho_rp = 0.5` version originally proposed. Ranking against the fixed peer set is the correct
answer for the universe the user actually supplied; resampling *which peers exist* is a separate,
later question (WORTH CONSIDERING).

Two known interactions: `apply_hysteresis` (`scoring.py:131-144`) can pin a letter that disagrees
with the modal draw — report both; and 300 draws is thin for tail letters, so state the resolution
or raise the draw count.

**Verify:** this is unfalsifiable without DO NEXT W2.1 (the calibration harness) — **ship them
together**. Target: empirical coverage in [0.85, 0.95] at every masking level, versus the
0.697→0.395 measured today. Also assert the reported point score falls inside its own interval and
that `sum(grade_probs) == 1`.

---

### 10. Make the `robust_z` clip adaptive instead of a fixed ±3 MAD

**Impact: high · Effort: small · `robust_z` is the shipped default in every profile**

`normalize.py:49` sets `_Z_CLIP = 3.0` and `_to_score` clips to it, so a large fraction of a real
universe lands at **exactly** 0 or **exactly** 100 and becomes mutually indistinguishable.

Measured on the real SEC CY2023 top-200-by-assets cross-section — fraction at exactly 0 or 100:
`roe` **17.3%**, `rnd_intensity` 15.0%, `asset_turn` 10.7%, `debt_assets` 10.5%, `roa` 10.1%,
`net_margin` 8.6%. On the full 6,719-filer universe the margin ratios reach **21-28%**. Excess
kurtosis of `roe` is 137 in the top-200 alone, so a 3-sigma fence is nowhere near rare here.

**File:** `src/stock_grader/normalize.py`

**Sketch:**

```python
z = (values - med) / (_MAD_CONSISTENCY * mad)
clip = float(np.clip(np.nanpercentile(np.abs(z), 99), 3.0, 12.0))
scores = _to_score(z, clip=clip)          # at most ~2% of any universe can saturate
# record `clip` on the metric so the report can show it
```

**Take only this half of the finding.** Do **not** change the default normalizer to
`gaussian_rank`: that would make the *absolute* half of the hybrid curve rank-based too
(`absolute_weight = 0.5`), removing the defence against a uniformly-bad universe manufacturing an
A. And skip the `n/(n-0.8)` MAD correction — a uniform cross-sectional scale factor worth 0.4% at
n=200.

While in this file, fix the 6-line adjacent defect: `normalize.py:101-103` falls back to plain
`zscore` when `MAD == 0` — i.e. to the outlier-sensitive estimator `robust_z` exists to avoid.
Use a biweight scale instead.

**Verify:** assert the exactly-0/exactly-100 fraction is ≤ 3% for every metric on the
default universe (currently 17.3% for `roe`); assert rank order is unchanged (this is an affine
map plus a clip, so it must be); assert the clip value appears in `explain`.

---

### 11. Report *effective* pillar weights, not the nominal ones

**Impact: high · Effort: small · The explanation layer currently asserts a provenance the score does not have**

`aggregate.py:49-70` `align_and_renormalize` silently drops NaN pillars, but `pipeline.py:401`
reports the full **nominal** `pillar_weights`.

Measured on a price-free run: the **momentum** profile grades ABT **B+ (74.84)** while printing
`{momentum: 0.50, risk: 0.20, liquidity: 0.06, growth: 0.14, profitability: 0.10}` and realising
contributions from **growth and profitability only** — 76% of nominal weight inert.
`deep_value` grades B− with its 0.55-weighted valuation pillar contributing nothing. This is the
most damaging place for a wrong number, because it is the layer a user checks to audit a grade.

**Files:** `src/stock_grader/pipeline.py`, `types.py`, `report.py`, `tests/test_pipeline.py`

**Sketch:**

```python
live      = {p: pillar_weights[p] for p in objects}      # objects = finite pillars only
total     = sum(live.values())
effective = {p: w / total for p, w in live.items()}
lost      = 1.0 - total
report.effective_pillar_weights = effective
report.lost_weight = lost
```

Render both in `report.py`. **Expose the refusal threshold on `GradeConfig` rather than hardcoding
`MAX_LOST_WEIGHT = 0.50`** — it flips several profiles to N/A on every price-free run, and
`consensus_grade` needs checking against profiles that decline.

While here, stamp `meta['pillar_set'] = sorted(pillar_matrix.columns)` plus a short hash, so a
cached B+ from a 6-pillar run is machine-distinguishable from a B+ from a 10-pillar run. (The
prose warnings at `pipeline.py:346-363` already cover the human reader; this is for JSON
consumers.)

**Verify:** assert the momentum profile on a price-free run reports `effective_pillar_weights`
summing to 1.0 over `{growth, profitability}` and `lost_weight ≈ 0.76`; assert nominal and
effective are identical on a full-coverage synthetic run.

---

### 12. Guard historical universes against survivorship, and add `fetch_by_cik`

**Impact: high · Effort: trivial**

Three merged findings, one guard plus one entry point.

- **Ticker-map filtering.** `resolve_cik` (`sec.py:451-454`) reads
  `www.sec.gov/files/company_tickers.json`, which lists only **currently-listed** issuers.
  Measured: **68.4%** of CY2012Q1I `Assets` filers are absent from today's map (14.1% for 2025).
  *(Honest caveat: that frame includes debt-only registrants and funds that never had a ticker, so
  part of the gap is "never listed", not "delisted". Direction and mechanism are unambiguous.)*
- **Ticker reuse — the sharper hazard.** `BBBY` resolves today to CIK 1130713, "BED BATH &
  BEYOND, INC." — the Overstock entity that bought the brand out of bankruptcy. The retailer that
  actually failed is CIK **886158**, now "20230930-DK-Butterfly-1, Inc." with no tickers.
  SIVB, WE, PRTY, REV, TUP, YELL, NKLA, RIDE, SBNY and FRC all resolve to `None`. A ticker-keyed
  historical run silently grades the survivor in place of the casualty.
- **The bundled universe is not neutral.** Measured: of 7,018 CY2015Q4I filers, 45.5% still filed
  at CY2025Q1I; survivor median ROA **+1.01% vs −6.15%**, leverage **0.606 vs 0.725**, survival
  58.3% profitable vs 34.6% loss-making. The grader then reports where a company sits on exactly
  profitability and health, with the left tail deleted.

**Files:** `src/stock_grader/cli.py`, `data/sec.py`, `config/universe_default.txt`,
`docs/design/DATA-GROUND-TRUTH.md`

**Sketch:**

```python
# cli.py::_resolve_peers — refuse the default list for a historical run.
if args.asof and (date.today() - args.asof).days > 365 and not args.universe:
    raise SystemExit(
        "config/universe_default.txt is a survivor list built from today's listings; "
        "pass --universe explicitly for a historical run")

# cli.py::_build_snapshots — one loud aggregate warning, not N scrolling notes.
missed = [t for t in tickers if resolve_cik(t) is None]
if args.asof and missed:
    warn(f"{len(missed)}/{len(tickers)} tickers unresolvable against today's ticker map "
         f"({', '.join(missed[:8])}...). 68.4% of 2012 filers are absent from it; "
         f"survivors had materially higher ROA and lower leverage.")

# sec.py — new entry point; fetch() becomes resolve-then-delegate.
def fetch_by_cik(self, cik, *, ticker=None, asof=None, pit_mode=PitMode.LATEST): ...
```

Key historical fixtures on **CIK**, and assert the resolved CIK so a ticker reuse trips a test.
Add a line to `DATA-GROUND-TRUTH.md:204` — it currently notes only that `company_tickers.json`
lacks index membership and market cap, not that it is survivor-filtered.

**Verify:** assert `resolve_cik("BBBY") == "0001130713"` **and** that a fixture keyed to the
failed retailer uses 886158; assert a `--asof 2015-01-01` run without `--universe` exits
non-zero; assert `fetch_by_cik(886158)` returns non-empty fundamentals.
(`scripts/validate_distress.py` already bypasses `resolve_cik` by calling `company_facts(cik)`
directly, so the capability exists — this makes it supported and warns on the unsafe path.)

---

# DO NEXT

Grouped into workstreams so related edits land together. Roughly two to four weeks. Impact is
still high — these are here rather than in DO NOW because they are **medium effort**, or because
they are **gated** on something above.

## W1 — Data resolution and history *(the deepest source of error)*

**W1.1 · Resolve each concept per period, gated by an equivalence class.** `sec.py:360` picks one
tag per company for all time (`next((t for t in chain if t in gaap), None)`). Measured: **293
company-concept pairs** across 147 cached payloads are frozen more than a year behind an available
lower-priority tag. Median annual revenue observations rise **7 → 17** once all chain tags are
used; companies with fewer than five annual revenue points fall from 44 to 26 of 141.

But naive union is measurably dangerous: merging the revenue chain produces **113 seams, 15.0% of
which show a YoY step above 50%**, against a 7.8% within-tag baseline. AXP jumps **+357%**
(2014→2015) and +282% (2007→2008); P&G +141%; AMT +93% — purely from tag switches, and those steps
feed `revenue_cagr_5y` and `revenue_growth_consistency` as real observations.

So the two halves are one item and must ship together:

```python
# concepts.py — annotate every chain entry
("Revenues", EXACT), ("SalesRevenueNet", APPROX), ("InterestAndDividendIncomeOperating", LAST_RESORT)

# sec.py — merge period-by-period, highest-priority tag wins each period
def splice_ok(base, cand, cls):
    ov = base.index.intersection(cand.index)
    if len(ov) >= 4:
        tol = 0.02                      # NOT 0.0+1e-9 even for EXACT — restatement rounding
        return float(np.median(np.abs(cand[ov]/base[ov] - 1))) <= tol
    if cls is LAST_RESORT: return False
    steps = base.diff().abs() / base.abs().shift()
    typical = float(steps.median()) if steps.notna().sum() >= 2 else np.nan
    seam = abs(cand.iloc[0] / base.iloc[-1] - 1)
    return seam < 0.50 and (np.isnan(typical) or seam < 3 * typical)
```

Three defects in the originally-proposed `splice_ok` are corrected above: the EXACT tolerance was
`0.0 + 1e-9` (rejects genuine aliases on rounding); `typical` divided by zero and returned `nan`
on short series, and `nan` comparisons silently return `False`; and the seam endpoints were
referenced but never derived.

*Keep `tag_used` as `dict[str, str]`* holding the tag that won the **most recent** period — its
only two consumers are `tests/test_sec_parsing.py:228` and `sec.py:488` — and add a separate
`tag_provenance: dict[str, pd.Series]` for full auditability. **Calibrate on the cached payloads
until the post-splice >50%-step rate matches the 7.8% within-tag baseline.** Have `history()`
refuse growth windows that cross an accepted seam. The ~40 equivalence-class assignments are hand
work and cannot be automated — budget them honestly.

**W1.2 · Detect stock splits and restate onto one basis.** Nothing in the codebase mentions
splits. NVDA in PIT mode at `asof=2024-09-01` alternates pre/post-split quarters, giving
`ttm shares = 1.3706e10` and `ttm eps = 17.51` against a true ~2.5. **It is live in LATEST mode
too**: annual `eps_diluted` carries three split bases in one series (6.63, 1.73, 0.17, 4.90), and
`fundamental.py:629` `share_count_change` returns **+0.5948** for NVDA at PIT 2022-06-30 —
reporting 59% annual dilution for a company that did not dilute.

Flag a QoQ share ratio within 3% of an integer (or 3/2), **corroborated by `eps_diluted` moving by
≈1/k in the same period**. Apply the cumulative factor backwards to `shares_basic`/`diluted`,
`eps_basic`/`diluted` and `snap.shares_outstanding`; record in `snap.meta['splits']`. In PIT mode,
detect only from filings with `filed <= asof`. **Drop the equity-veto** proposed alongside this —
a split does not move `StockholdersEquity` at all, so the veto never fires and cannot discriminate.

**W1.3 · Scale-consistency pass with a DEI cross-check.** MCD tags diluted shares in *millions*
from its 2024 filings onward while every earlier filing used units; latest-filed selection picks
the scaled-down vintage, so `ttm('shares_diluted') = 716.3` and implied EPS is **12,115,036**. The
DEI tag reads 710,505,859 on the same date, proving it in one comparison. NVDA has the same defect
in 2009-2010 (5.4e5 among 5.5e8 neighbours).

Per-concept modal-decade check over the chosen records: take `median(log10|value|)`; snap records
more than 1.5 decades away by the nearest power of 1000 when within ~0.3 of an exact `10^3k`,
else drop. Require ≥8 records supporting the modal decade; whitelist concepts. For share counts,
the decisive independent test is one line:
`reject when |log10(ttm_shares / dei_EntityCommonStockSharesOutstanding)| > 0.5`.
*Note market cap survives today because `sec.py:498` prefers DEI — the damage is to per-share work
and to any window crossing the discontinuity.*

**W1.4 · Post-build sign-island detection.** MCD `StockholdersEquity` at 2018-03-31 is
−4,718,800,000 in two filings and **+4,718,800,000** in a third; latest-filed wins, producing the
single positive point in an otherwise negative run and flipping P/B, ROE and Altman Z for that
quarter. For persistent-sign concepts, flag any period whose sign differs from **both** neighbours
while its magnitude is close to their mean, and drop it with a warning. Use a **relative**
tolerance for the majority vote across vintages: the exact-equality guard originally proposed
catches MCD but misses its own second example (WMT tax, 7,139 vs 7,156). Do this **after** the
series is assembled — `_select` runs per-period at `sec.py:239`, before any series exists, so
there are no neighbours to read.

**W1.5 · Record restatement metadata, and use it to gate Q4.** Measured: **1,395 of 25,647**
periods carry more than one distinct value (5.44%), 367 differ by more than 10%, 3 are exact sign
inversions. Band at 2% silent / 10% report / >10% **block Q4 derivation for that fiscal year**.
This is a prerequisite for the strong form of DO NOW 6 — it is the test that actually catches
Target (FY2013 capex moved 45% across vintages), whereas the filed-date guard does not fire.

**W1.6 · Cash-flow articulation check.** Ship **only this one** of the three cross-statement checks
proposed. Comparing `cfo + cfi + cff` to the balance-sheet cash series gives a median relative
error of **0.76-1.00** across all ten sampled filers — a checker that fails on everything. Against
the filer's own reconciliation tag
(`CashCashEquivalents...PeriodIncreaseDecreaseIncludingExchangeRateEffect`) plus the FX tag, the
median error is **0.0000** and the 1% test passes on 100% of years for 8 of 10 filers. On breach,
mark `cfo`/`cfi`/`cff` suspect for that year (which correctly kills `fcf` too); treat an isolated
FY2018 breach as a warning (ASU 2016-18 transition).

*Drop the companion D&A-rate and interest-vs-debt bands:* the D&A band `[0.02, 0.45]` produces
**3 of 10 false positives** (MCD 0.013, ABBV 0.008, BLK 0.004), and the interest band
`[0.005, 0.25]` fires on non-banks **TGT (0.858)** and XOM (0.303) — and it was justified using
measured interest/**revenue** ratios while being proposed on interest/**debt**.

**W1.7 · `_derive` as a fixpoint solver over true identities only.** Move the nine single-pass
if-statements (`sec.py:399-437`) into `data/derive.py` as signed linear identities solved to a
fixpoint, each derived cell stamped with its rule id, depth capped at 2. Measured marginal
coverage on the cache for rules not implemented today: `income_tax` +7 companies, `operating_income`
+7, `eps_diluted` +7, `pretax` +6, `cogs` +5.

**Include only genuine identities** — balance-sheet balance, asset/liability splits, NCI, gross
profit, the tax bridge, total debt, net debt, working capital. **Explicitly exclude as derivation
sources:** (a) the pretax↔EBIT interest bridge — it assumes away other income, equity-method
income and FX, and it was the *largest* claimed gain (12%) feeding `interest_coverage` directly;
(b) `gross_profit − operating_expenses`, which fails for filers whose `OperatingExpenses` excludes
or double-counts COGS; (c) the `{'net_income': 1, 'eps_diluted': -1}` entry, which is
dimensionally invalid and would be executed by the solver loop before any prose caveat applies.
Run the same equations in **reverse** where all terms are known and warn on residuals above ~1% of
scale — that residual check is the automated form of the check that caught the REIT
EBITDA-below-EBIT bug, and it is the best idea in the finding.

**W1.8 · Same-source annual fallback for TTM flows.** `ttm()` returns `None` while an annual
observation exists for `interest_expense` in **14%** of companies, `ebitda` 13%, `income_tax` 13%,
`D&A` 12%, `revenue` 11%, `capex`/`cogs`/`fcf` 10%. Because `ebitda` and `interest_expense` feed
`ev_to_ebitda`, `net_debt_to_ebitda` and `interest_coverage`, roughly one company in eight loses a
whole leverage sub-pillar. Add `ttm_or_annual(concept, asof, max_staleness_days) -> (value,
source)` **plus a paired accessor** so a ratio's numerator and denominator never straddle sources —
a numerator from last quarter over a denominator from a fiscal year ending 14 months ago is worse
than `MISSING`. **Re-measure the gap after W1.1 and DO NOW 1 land**; some of it is downstream.
Defer the `MetricResult.quality` float and its plumbing through four modules until the value is
demonstrated.

## W2 — Uncertainty *(gated: build the harness first)*

**W2.1 · A T1 calibration harness — build this before anything else in W2.** `tests/test_invariants.py:256-286`
contains only monotonicity, coverage-monotonicity and determinism, all of which a badly
miscalibrated interval passes trivially. Add `bench/calibration.py` plus one pytest gate: build a
factor-structured pillar panel, define truth as the full-data score, mask components at
k ∈ {0.1, 0.2, 0.3, 0.4}, recompute the degraded score **and its degraded interval**, and report
empirical coverage plus the Gneiting-Raftery interval score
`(hi − lo) + (2/0.10)·max(0, lo − truth) + (2/0.10)·max(0, truth − hi)`. Assert coverage in
[0.85, 0.95]. Pure numpy, no network, seconds to run — **and it already fails**, which is the
point. Every other change in W2 is unfalsifiable without it, and several move coverage in
*opposite* directions, so shipping them blind risks making things worse.

Skip the PIT histogram, the KS test and the (condition × N × profile) acceptance grid proposed
alongside it — that is a research programme, not a test.

**W2.2 · Replace the coverage multiplier with a finite-population variance.** `scoring.py:147-157`
uses an undefended `1 + 2(1−c)²` and applies it **multiplicatively** at `scoring.py:233-235`, so
the amount by which missing data widens a grade is proportional to how much the pillars happen to
disagree — two companies at identical 50% coverage get different missing-data penalties for
reasons unrelated to missing data. Measured multiplier: 1.020 (c=0.9), 1.500 (c=0.5), 1.845 at the
0.35 grading floor, 3.0 at c=0. (Note it is monotone increasing, not "saturating at 1.85" as one
audit claimed.) Replace with the exact sampling variance of a mean over `m = c·n` of `n`
components drawn without replacement, and compose in variance:

```
se_missing = s_within * sqrt((1/c - 1) / (n - 1))
se_total   = sqrt(se_resample**2 + se_missing**2)
```

No fitted constant. Plumb metric-level dispersion up from `build_pillar_score` so `s_within` is
measured over metrics (n = 40-105), not pillars (n = 4-10). Also delete the unused `floor=0.4`
parameter, which is never referenced in the body and silently disagrees with
`MIN_COVERAGE_TO_GRADE = 0.35`.

**W2.3 · Give each pillar a standard error and inject it into the resample.** Every pillar score
currently enters `uncertainty_interval` as an **exact number** (`pipeline.py:379-388` passes bare
point scores), so a liquidity pillar resting on 1 surviving metric of 3 gets the same interval as
one resting on all 3 — even though `PillarScore` already carries `coverage`, `n_metrics`,
`n_missing` and `metric_scores`. Pillar sizes range from 3 (liquidity) to 24 (risk).

```
se_choice = s_p / sqrt(k_eff),   k_eff = k_p / (1 + (k_p - 1) * rho_bar)
```

The `k_eff` correction is **mandatory, not optional**: 24 risk metrics are not 24 independent
draws when many are transforms of the same return series, and without it this understates exactly
the large pillars. `rho_bar` is one line on the existing scores DataFrame.
`_vectorised_power_mean` does `weights @ values` (`scoring.py:265-273`); generalising to a
`(draws × n)` value matrix is an elementwise product plus a row-sum.

**W2.4 · Reparameterise `leave_out`, and recentre the interval.** `scoring.py:195`
`keep_n = max(2, int(round(n*(1-leave_out))))` is a step function that does nothing across its
plausible range — measured half-widths at n=8: **2.126** for `leave_out ∈ {0.00, 0.05}`, **3.179**
for {0.10, 0.125, 0.15}, **4.157** for {0.20, 0.25, 0.30} — and drops **zero** components at n=2
or 3. (Note the registry has **10** pillars and n=10..13 drops **2**, so exact leave-one-out is
not a drop-in replacement.) Worse, subsets are drawn uniformly regardless of weight, so
`deep_value`'s 0.55-weight valuation pillar is discarded in 25% of draws, producing a bimodal
sample on which a 5th/95th percentile is not a meaningful summary.

Replace with a weighted jackknife: weight replicate *i* by the mass `w_i` it removes,
`var_jack = ((n-1)/n) * Σ w_i (θ_(i) − θ̄)² / Σ w_i`. Separately, **one line**: replace
`centre = float(np.median(finite))` (`scoring.py:232`) with the actual point estimate. Measured
`|point − midpoint|`: median 2.18, p95 **12.91**, max 23.34 points with `deep_value` weights —
against 6-point letter bands, an interval displaced by 12 points spans entirely the wrong letters
while nominally containing its own score. Also replace the hard-coded `half = 5.0` in the
single-metric branch (`scoring.py:187-190`).

**W2.5 · Two small hardening fixes in the same file.** (a) Raise the Dirichlet alpha floor from
`1e-3` to **0.5** (the Jeffreys prior) and change `out = np.zeros_like(...)` to
`np.full_like(..., np.nan)` at `scoring.py:208-211`, so a degenerate all-zero weight row is
dropped by the existing `isfinite` filter instead of evaluating to a finite **0.0** (or `exp(0) =
1.0` under `geometric_mean`) and landing in the 5th percentile. Reproduced: point estimate 70.000
with a reported CI of **(0.000, 70.000)**. Degeneracy rises from 0.06% at min-weight 1e-3 to 18.7%
at 1e-5. Under shipped defaults the smallest pillar weight is ~0.08 so this never fires today — it
is downstream of W3.1 — but it is three one-liners that close a fabricated-zero path permanently.
(b) Move `concentration` out of the `scoring.py:167` default into `GradeConfig`, set it from the
observed spread **across weighting methods**, and record it in `GradeReport.meta`. Measured at n=8:
total half-width 3.164, of which Dirichlet-only **2.141** and metric-set-only 2.029 — the largest
single term, and a hard-coded guess. Note this **narrows** while DO NOW 9 **widens**: ship both
behind W2.1.

## W3 — Weighting statistics

**W3.1 · Gate sparse and all-NaN columns out of every dispersion computation.** `weighting.py:100-103`
`_clean` median-imputes and never drops columns, so a column observed twice in fifty becomes a
near-constant one. Measured: `inverse_variance` assigns it **w = 0.999992** while every real
metric gets ~0.000002, with `ctx.warnings` **empty**; `inverse_volatility` 0.993290. The
`.replace(0.0, np.nan)` guard at `weighting.py:294` misses it because the variance is 5.26e-29,
not 0.0. `min_variance`, `risk_parity`, `max_diversification`, `hrp` and `decorrelated` all inherit
it via `fallback="inverse_variance"`. With an all-NaN column, `np.cov` returns a 100%-NaN matrix
and `hrp`/`decorrelated` return silent **equal weights with no warning at all** — which is live on
every run today, because with no price feed every `needs_prices` metric yields an all-NaN column.

**Critical implementation note:** `min_variance` (`weighting.py:340`), `max_diversification`
(`:403`) and `decorrelated` (`:491`) build `pd.Series(result.x, index=X.columns)` where
`len(result.x)` comes off the covariance matrix — dropping columns inside `_covariance` makes that
a **length-mismatch ValueError**. Thread the surviving column labels through and let
`_normalize(..., X.columns)` zero-fill the rest.

*Do not apply the same patch to `coefficient_of_variation`:* measured weight on the same sparse
column is 0.000183. CV weights **by** dispersion, so a near-constant column gets near-zero weight;
CV blows up when `mu ≈ 0`, an unrelated condition.

**W3.2 · Replace the hand-rolled Ledoit-Wolf with the exact closed form.** `weighting.py:306` uses
`np.cov` (ddof=1) while `weighting.py:315` divides by `n²p` — the expression derived for the `1/n`
estimator. Systematic **under**-shrinkage, worst exactly where `p/n` is worst, which is the
pillar-level case. Verified against `sklearn.covariance.ledoit_wolf_shrinkage` on seven shapes:
at n=10,p=12 ours **0.5235** vs correct 0.6443; n=6,p=12 **0.5301** vs 0.7536; n=30,p=50 **0.9069**
vs 0.9705. The proposed O(np) form matched sklearn to every printed digit on all seven, and
replaces an O(n·p²) Python loop:

```python
Y  = X - X.mean(0);  S = Y.T @ Y / n;  mu = np.trace(S) / p
d2 = ((S - mu*np.eye(p))**2).sum() / p
b2 = min(((Y**2).sum(1)**2).sum()/(n**2 * p) - np.trace(S @ S)/(n * p), d2)
k  = b2 / d2                       # scale-invariant: every consumer is invariant to a positive scalar
```

Golden weight values will move. Add a test asserting agreement with sklearn to 1e-9.

**W3.3 · Estimate association without imputing first.** Measured on the real 200-name SEC score
panel (38.6% NaN): mean |off-diagonal correlation| is **0.218 imputed vs 0.326 pairwise-complete**.
Individual pairs are off by up to 0.54 (`cfo_margin`/`rnd_intensity` +0.085 vs **+0.624**) and one
**flips sign** (`asset_turn`/`rnd_intensity` +0.149 vs −0.219). CRITIC's entire purpose
(`weighting.py:209-222`) is to discount redundancy via `Σ(1 − r_jk)`, so understated correlation
makes redundant metrics look independent — the exact failure it was written to prevent. Imputation
also crushes dispersion (`op_margin` sd 21.6 observed vs 11.5 imputed).

Two distinct fixes:
- **Dispersion/covariance family** (`critic`, `hrp`, `pca`, `_covariance`): pairwise-complete
  correlation with `min_periods`, fill never-co-observed pairs with 0 and warn, **eigenvalue-clip
  to PSD** (Higham is unnecessary at p ≤ 30), rescale by observed-only column sd. Apply W3.1
  first so low-overlap pairs never enter.
- **Supervised family** (`ic`, `regression`, `shapley`, `mutual_information`): compute on each
  metric's own complete-case index, require `n >= 20`, and weight by `ic_j · sqrt(n_j − 1)` so
  estimates of differing reliability combine correctly. Measured attenuation at true IC 0.29:
  ratio **0.84 / 0.64 / 0.48** at coverage 0.75 / 0.50 / 0.30 — roughly **linear in p**, not the
  `p^1.5` originally claimed (which would predict 0.65/0.35/0.16, far too aggressive). Store
  `ic_n` so a report can say a metric got low weight because it was measured on 12 names.

Also shrink the IC vector on the **Fisher-z** scale before clipping negatives (James-Stein toward
the grand mean, `se² = 1.06/(N−3)`, `tanh` back). Reproduced under a strict null with 12 noise
metrics: `E[max weight] = 0.357 / 0.360 / 0.375` at n = 20 / 50 / 200 against 0.083 for equal, and
~6 of 12 metrics zeroed at every n — **flat in n**, so the estimator converts sampling noise into
weight and never improves. Skip the Newey-West half: `ctx.config['ic_history']`
(`weighting.py:545`) has **no producer anywhere in the repo**.

**W3.4 · Two trivial weighting fixes.** (a) `bagged` — raise `bagged_rounds` from 20 to 200, and
make `bagged_base` fall back to the method the caller **actually requested** rather than
hard-coding `'critic'`. Measured on the real 200-name panel across 8 seeds: seed-to-seed total
variation is **0.0087 at 20 rounds** against a leave-one-security-out sensitivity of **0.00126**
for `critic` itself — the Monte-Carlo noise bagging injects is 7× larger than the sampling
instability it exists to average away (0.0043 at 100 rounds, 0.0022 at 400). *Two sub-claims from
the source finding are wrong and should not be patched:* the RNG is already seeded from `ctx.seed`
(`weighting.py:767`), and `ctx.warn` already dedupes (`:77-78`) — 0 warnings accumulated after 50
rounds. (b) `fixed_weights` (`weighting.py:122-129`) — diff `ctx.fixed`'s keys against
`X.columns` and `ctx.warn` on any key that matched nothing, with a `difflib.get_close_matches`
suggestion. Reproduced: one character wrong (`'valuations'`) silently yields valuation 0.0,
profitability 0.4, health 0.4, growth 0.2 with `ctx.warnings == []`, and
`test_weights_satisfy_contract` passes throughout because sum=1, non-negative and finite all hold.
Keep "a component got zero weight" as `log.info`, not a warning — every profile deliberately omits
pillars.

## W4 — Price unlocks *(see Creative workarounds for the capability story)*

**W4.1 · Add a `StockAnalysisPriceProvider` ahead of Yahoo in the chain.** Detail in Creative
workarounds 1.

**W4.2 · Populate `snapshot.benchmark`.** Detail in Creative workarounds 2.

**W4.3 · Make price provenance first-class.** Add `price_date` and `price_source` to
`SecuritySnapshot`, stamp them on **all three** branches in `_build_snapshots`, default a bare
`--price` to today with an explicit warning, carry them into `GradeReport.meta`
(`pipeline.py:419-424` currently carries only sector/pit_mode/curve/coverage_penalty), and print
price date plus age next to the grade. Today staleness is handled on exactly the one branch that
already thought about it (`cli.py:158-167`, 60-day warning, 400-day refusal) and nowhere else: the
fetched-series and manual-price branches carry **no date at all**, and none of it reaches
`GradeReport`, so `report.py` cannot mention price age even when it is known. Skip the
`sigma_gap`-into-the-interval machinery — it needs an annualised volatility the scalar-price path
cannot supply.

**W4.4 · Reject price series that are not daily or not current.** Every price metric windows
**positionally** — `_total_return` uses `prices.iloc[-1-skip-days]` (`statistical.py:475-483`),
`dollar_volume` uses `.iloc[-63:]`, annualisation is a hard-coded `np.sqrt(TRADING_DAYS)` — and
`_prices` (`:33-38`) only `dropna()`s. So a weekly or gapped CSV, the drop-in path the docs
actively advertise, returns a 2-year return labelled `momentum_3m` with volatility scaled √5 too
high. Do the cheap half first: compute the median calendar spacing of the index and return `None`
outside ~[0.5, 1.6] days, **and reject when the newest bar is more than a few business days before
`snapshot.asof`** — that second check is the strongest sub-case and the cheapest, because a daily
CSV that simply ends two years ago currently produces a fully confident, correctly-scaled,
two-years-stale momentum reading. Change `engine.py:60` from `len(snapshot.prices)` to
`np.busday_count` over the index span so "trading days" means what it says. The calendar-anchored
rewrite of `_total_return` is the follow-on, not the first step. *Structurally identical to the
already-fixed `ttm()` contiguity bug.*

**W4.5 · Guard `EntityPublicFloat` against scale errors.** `sec.py:500-505` stores it with no
magnitude check, and when no insider price exists it flows through `implied_price_from_float`
straight into `snapshot.price` (`sec_prices.py:314-321`, `cli.py:156`). A 1000× in-thousands scale
error **survives the existing defence**: `calibrate_non_affiliate_fraction` rejects `fraction >
1.15` (`sec_prices.py:402-404`), which merely routes the bad value down to the *uncorrected*
lower-bound branch instead of stopping it. Parse shares first, compute `implied = value / shares`,
and reject with a warning outside `[0.02, 5000]`. *(The "never use float as a price" half is
already the code's position — it is documented as a lower bound and warns. The magnitude guard is
the whole value here. I could not re-verify the 0.7% error-rate figure offline; the guard is worth
adding on its own logic.)*

**W4.6 · Close the two residual gaps in the insider-price loader.** Add `SECURITY_TITLE` and
`FILING_DATE` to the `usecols` at `sec_prices.py:152-158`. Filter titles to common/ordinary and
exclude `class b|class c|preferred|warrant|unit|depositary` before the per-day median — without
it, a multi-class issuer's preferred transactions land in the same median as its common, priced
against the common share count. Clamp `TRANS_DATE <= FILING_DATE`, and in `price_series` filter on
`filed <= asof` rather than transaction date alone — otherwise `--pit` uses trades executed before
`asof` but not disclosed until after it, exactly the look-ahead `PitMode.PIT` exists to prevent.
Small deltas to already-working, already-tested code.

## W5 — Sector coverage *(see Creative workarounds 5)*

**W5.1 · Bank-native concept and metric pack.** **W5.2 · REIT FFO reconstruction.**
**W5.3 · Define revenue once per business model.** All three detailed in Creative workarounds.

## W6 — Metric and model correctness

**W6.1 · Bias-correct the Hurst estimator (Anis-Lloyd).** Best impact-per-line in the entire
review: two lines plus a memoized helper. `statistical.py:393-396` fits the OLS slope with no bias
correction, so a **true random walk returns 0.599** (bias +0.099) and only **48.5%** of genuine
random walks land inside the metric's own `(0.45, 0.60)` ideal band — roughly half of ordinary
stocks are penalised for "trending" by an artefact of the estimator, scored through
`double_sigmoid` at full weight with no error raised. With the correction: mean **0.4994**, in-band
**92.0%**. Subtract the theoretical iid expected `log(R/S)` per scale before the slope, then add
0.5 back; `lru_cache` the helper — at T=756 only five scales (8/16/32/64/128) are ever used, so it
is free. *Regression test that fails on current code:* assert the estimator returns 0.50 ± 0.02 on
`rng.normal(0, 0.018, 756)` averaged over 200 seeds.

**W6.2 · Make a missing risk-free rate a coverage fact, not a silent 0%.** `statistical.py:54-56`
returns `pd.Series(0.0)` when `s.risk_free` is None — directly contradicting its own module
docstring at lines 9-11 ("a different statistic, and a flattering one"). Reachable:
`cli.py:101-103` sets `risk_free=None` under `--no-network`, and `RiskFreeProvider.get` returns
`None` on any HTTP failure even online. Add `needs_risk_free: bool` to `MetricSpec` (mirroring the
existing `needs_benchmark` pattern at `engine.py:67-70`), declare it on `sharpe_ratio`,
`sortino_ratio` and `capm_alpha`, and short-circuit to `MISSING`. Replace the trailing
`.fillna(0.0)` with `.bfill()`. Keep an explicit, labelled `GradeConfig.assume_zero_risk_free`
escape hatch. The bias is `rf_daily / sigma_daily * sqrt(252)`, so at DTB3 = 5.3% a 25%-vol name
gains **0.21** of Sharpe while a 60%-vol name gains 0.09 — vol-dependent, therefore
**rank-changing**, on a cross-section whose Sharpe dispersion is ~0.7.

**W6.3 · Stop fabricating neutral components in the published models.** Three merged findings in
`models.py` / `fundamental.py`:

- **Piotroski rescaling.** `fundamental.py:521-523` returns `points * 9.0 / counted`, so a company
  whose data supports only 5 tests and passes all 5 scores **exactly 9.0** — indistinguishable
  from a company that passed all nine. `Var = 81·p(1−p)/counted`, so sd is inflated by
  `sqrt(9/5) = 1.34×`, and `P(score = 9)` is `p⁵` vs `p⁹` — **7.8% vs 1.0%** at p=0.6. Replace with
  the pass proportion shrunk toward 0.5 by `counted/(counted + k)`; state `k` in the docstring as
  the judgement call it is.
- **Beneish TATA.** Apply the precedent this project already validated once: `accruals_ratio`
  returns `None` for loss-makers (`fundamental.py:597`) because a large loss drives NI − CFO
  negative and a lower-is-better metric reads it as conservative accounting. The `+4.679·TATA`
  term — the **largest coefficient in the model** — has the identical disease and no gate.
  Measured: TATA is **−0.117 (BBBY)** and **−0.119 (TUP)**, both months from Chapter 11, against
  −0.002 (HD) and −0.047 (ABT). A big writedown reads as pristine earnings quality.
  ***Do not*** raise the component gate from 6 to 8 as originally proposed — it costs coverage
  (26 → 18 computed on a real panel) without touching the mechanism. And validate against the
  **restatement** label `validation.py` already supports, not bankruptcy.
- **Altman Z''.** `models.py:232-237` has no clamp and no confidence flag. Measured: **BBBY scores
  Z'' = 5.21** at `asof=2022-06-30` — deep inside the "safe" region above 2.6 — with RE/TA 1.881,
  book equity **−220,298,000** and negative TTM EBIT, ten months before Chapter 11. Treasury stock
  is contra-equity under US GAAP, so buybacks leave retained earnings enormous while equity goes
  negative, and Altman's estimation sample had no such firms. Winsorise RE/TA to `[−1, 1]`, expose
  the components, and set `low_confidence` when `equity < 0` or `|RE/TA| > 1`. **Reject
  `min(RE, equity)`** — it falsely distresses Home Depot. Note TXN scores 12.90 with RE/TA 1.862
  while perfectly healthy, so the clamp is mitigation and the flag does most of the work; demote
  Z'' below Ohlson. **Resolve the return-type conflict first:** this changes the signature from
  `float | None` to a tuple, and another proposed test calls it as a bare float — check how
  `engine.py:86-88` unpacks tuple returns before committing.

**W6.4 · Floor the power-mean and evaluate it in log space.** `aggregate.py:123/133/156` clip at
`_EPS = 1e-12`, and `align_and_renormalize` (`:66`) admits **exactly 0.0** — which is reachable on
the default path, because `robust_z`'s `_to_score` returns exactly 0.0 for any name 3+ MAD below on
every metric in a pillar. Measured with valuation weight 0.40: geometric composite **0.000185 at
pillar = 0.0 vs 11.665 at 1.0** — a 63,000× swing for a one-point difference, magnitude set
entirely by an arbitrary constant. Two shipped profiles use `rho = 0.0` (`value`) and `rho = −0.5`
(`deep_value`) where it is worst. Set a stated `_SCORE_FLOOR = 1.0` on the 0-100 scale (a policy
change — say so in the docstring), route all three through one `_power_mean` helper using
`logsumexp`, and validate `--rho` at the CLI boundary (`ces` with rho=200 currently returns `None`
via the fail-soft path rather than naming the bad parameter). Use `pytest.approx` against stored
values; `test_ces_rho_controls_compensation` may need its tolerance revisited.

**W6.5 · Reject Cornish-Fisher VaR outside its domain.** Verified arithmetic at z = −1.6449:
`(skew 0, excess kurtosis 30)` → **CF_z = −1.039**, so a stock with kurtosis 30 has a *smaller* 5%
VaR than a Gaussian one — and `direction = −1` then scores it as the **safest stock in the
universe**. `(skew +5, kurtosis 30)` → **+0.851**, asserting the 5% worst day is a gain.
Monotonicity fails at (0,20), (0,30), (+5,30) and (−2,20). Not fringe: `excess_kurtosis`'s own
winsor is `(−5, 30)`, and t(4) innovations at n=504 exceed excess kurtosis 30 in **3.1%** of
samples (13.8% under t(3)). Evaluate the map on a 60-point grid over z ∈ [−3, −0.5] and return
`None` unless strictly increasing. Pass `bias=False` to `stats.skew`/`stats.kurtosis` at
`:214/:230/:238`. Four lines. Ranked here only because `snapshot.prices` is unset today — it is
cheap insurance that fires the moment a price feed lands.

**W6.6 · Declare `winsor` on the 43 unbounded metrics.** `engine.py:97-102` already clamps to
`spec.winsor` **and emits a visible note**, so this converts an impossible value into an auditable
one for free, and it protects `robust_z` too. Suggested: `debt_to_assets (0,5)`,
`gross_margin (−2,1)`, `operating_margin`/`net_margin (−5,1)`, `roe (−5,5)`,
`roa`/`roic`/`croic (−2,2)`, `asset_turnover (0,20)`, `goodwill_to_assets (0,1)`,
`earnings_yield`/`fcf_yield (−5,5)`, `capex_intensity`/`rnd_intensity (0,5)`. Also a second line of
defence on DO NOW 5. While here, remove the two duplicated caps (`calmar_ratio` has
`safe_div(cap=10.0)` *and* `winsor=(-10,10)`; `tail_ratio` likewise).

**W6.7 · Seed synthetic prices from a content hash.** `synthetic.py:68`
`seed = abs(hash(ticker)) % (2**31)` sits directly under a docstring promising determinism. Three
separate subprocesses calling `generate_prices('AAPL', n_days=300)` returned final adjusted closes
of **63.80 / 136.38 / 97.08** — Python randomises string hashing per interpreter. This is the only
nondeterminism in the codebase, and 246 tests miss it because `generate_panel` passes `seed`
explicitly. Use `blake2b(f"{run_seed}:{ticker}")` (stdlib) and thread `GradeConfig.seed` through
from `cli.py:141`. **The regression test must spawn subprocesses** — an in-process assertion
cannot detect this.

## W7 — Validation infrastructure

**W7.1 · Make the existing distress harness an offline pytest.** Do **not** build a new harness —
`validation.py` and `scripts/validate_distress.py` already do labelling, PIT grading and AUC.
Vendor a small set of `companyfacts` payloads (store only the ~20 concepts the models need,
gzipped, keyed by **CIK**) plus a fixed label list, and add `tests/test_case_studies.py` running
`separation_report` offline with per-metric AUC floors. **Assert coverage separately** so a model
cannot "win" by returning `None` for the distressed names. **Size- and SIC-match the control group
before quoting any absolute AUC** — the current `CONTROLS` list is 30 mega-caps, which confounds
size with the label and feeds Ohlson's explicit `−0.407·log(assets)` term. Refine the label
collector to use the **structured `items` field** in `submissions.json` (verified: filtering on
"1.03"/"4.02" works cleanly) rather than full-text phrase matching — more precise, avoids the
10,000-result cap, needs no negation filtering.

**W7.2 · One frames client, three consumers.** `data.sec.gov/api/xbrl/frames` is verified
reachable (HTTP 200; `Revenues/CY2024Q1` = 319,680 bytes; `GrossProfit/CY2024` = 2,613 companies)
and already recorded in `DATA-GROUND-TRUTH.md:13`. It is unused in `src/`. Add one client and
three consumers:
- **Tag-coverage audit** (highest value): one GET per tag over 6,628 filers, compared against a
  checked-in baseline, failing on a >3pp drop. This is exactly how the `liabilities` gap was
  found. Every other validation idea runs on a 20-30 name panel that **structurally cannot**
  detect a coverage bug affecting 12% of filers.
- **Differential test of quarter assembly.** `normalize_duration_facts` (`sec.py:204-301`) does
  three-pass assembly, YTD differencing and Q4-by-subtraction; **four of the seven known bugs
  lived there**, and it is tested only against fixtures written by the same author whose
  understanding produced them. Frames is an independent, **currency-pinned** (uom in the URL)
  oracle that publishes calendar Q4 directly, so the FY-minus-Q1-Q2-Q3 derivation finally gets a
  real external check. Match periods within ±15 days for fiscal-vs-calendar drift; split the
  error distribution by whether the quarter was Q4-derived.
- **CIK-keyed PIT universe** (`--universe all-sec`), the only real fix for survivorship. Dead
  issuers work: CIK 106455 (Westmoreland Coal, bankrupt 2018) returns 1,881,697 bytes of
  `companyfacts` and 9 dated `EntityPublicFloat` records. Union 2-3 adjacent CY frames so
  off-calendar fiscal quarters are not dropped.

Vendor the payloads so the tests stay offline; mark any live check `@pytest.mark.network` and run
it on a **schedule**, not per-commit — the 246 existing tests are offline and must stay that way.
Frames are calendar-aligned, so coverage figures are a **lower bound** on true tag adoption.
**Skip the `peer_ratio_bounds` imputation half** — it is one careless call away from becoming the
point imputation that caused already-fixed bug #6.

**W7.3 · Runtime contract checks at the two chokepoints that pay.** No runtime assertions exist
anywhere in `src/`. Add `contracts.py` with a `ContractViolation`, and wire: `check_scores` at
`normalize.py:303` — assert the index is unchanged and, above all, that
`raw.isna().equals(scored.isna())` so a missing value can **never** become a number; and
`check_weights` / `check_aligned` — finite, non-negative, sums to 1.

**Scope this down from the original proposal, whose headline justification does not survive
checking.** "Six of the seven known bugs would have been caught" is false; by my reading it is
**two** — the flat-50 normalizer bug (NaN-pattern check) and the out-of-band CI (containment
assertion). Bugs 1, 2 and 4 all live in `sec.py`, upstream of every chokepoint, and bug 5
(`momentum_1m`) was a wrongly **declared** direction, which a consistency check passes by
definition. **Skip the direction-monotonicity check** — `sector_neutral` (`normalize.py:245-272`)
is not globally monotone by construction, and it cannot catch a wrong declared direction anyway.
Measure the `normalize.py` check on a 500-name universe before enabling it unconditionally (it
does a concat+sort per metric). The claimed 3.9% overhead is unverified.

**W7.4 · Metamorphic tests: permutation and idempotence first.** The suite has no properties
relating two runs, only properties bounding one. Add: reordering panel rows must not change any
grade; `grade_universe` twice on the same snapshots must be identical; and improving one cell must
not lower that row's score. Assert monotonicity **strictly** for `equal`/`critic`/`stddev`/
`inverse_variance` (measured 0 violations in 600 random panels) and as a **bounded-rate
regression** for `entropy` (5/600, 0.8%, worst drop 1.14 pts) and `pca` (1/600). Root cause is
`weighting.py:192` computing a whole-panel statistic that includes the row being scored. **Do not
implement leave-one-out weighting** — see Deliberately not doing.

**W7.5 · Ship the multiple-testing floor and MinBTL.** ~30 lines of pure functions:
`expected_max_ic_under_null(N, K) = sqrt(2·ln K / (N−1))`,
`min_backtest_years(N_configs, SR) = 2·ln N / SR²`, and Benjamini-Hochberg over per-metric IC
p-values using `z = rho·sqrt(n−1)`. Simulated under a strict null at N=82: `E[max|IC|]` is
**0.306** with 105 metrics and **0.245** with the 23 weighting methods alone — against published
cross-sectional factor ICs of 0.02-0.05. Any bake-off winner over this registry will look like a
world-beater by construction unless a floor is reported beside it. Use the closed form only as a
guard: it **overshoots** the simulated value by 15-25% at small K (0.339 predicted vs 0.306
simulated at K=105). MinBTL is the binding constraint nobody has written down: 105 metrics demands
9.3 years and the full 105×23 grid demands 15.6, while the honest XBRL window starts 2012
(CY2011Q1I has 2,379 filers vs 7,606 at CY2012Q1I) giving ~14 years. **Defer `deflated_sharpe`**
until a Sharpe time series exists, and **defer CSCV/PBO entirely** — `C(16,8) = 12,870`
recombinations before the harness that would give it something to split even exists.

## W8 — Truth in reporting

**W8.1 · Honour `previous_letters` and add the missing `--hysteresis` flag.** `apply_hysteresis` is
defined (`scoring.py:131`), exported (`:36`), named in `grade_universe`'s signature
(`pipeline.py:191`) and described in its docstring (`:199`) — with **zero call sites**. Measured on
a 30-name PIT panel across five consecutive quarter-ends: **11 / 21 / 15 / 15 of 30 letters change
per quarter** while Spearman holds at 0.875-0.958 — the ranking is stable and the letters are not,
so a user sees four signals a year where the view barely moved. Call it after the curve selects
the letter and **before** the coverage-based N/A override, so a refusal to grade is never
suppressed. **Keep the default off** per `SPEC.md` D9, which explicitly records hysteresis as
off-in-the-library, caller-supplied state — the fix is to honour the argument and add the flag
`SPEC.md` already specifies, not to enable it unconditionally. Do **not** encode the proposed 25%
churn ceiling as a CI assertion yet; those numbers are panel-dependent.

**W8.2 · Stamp in-sample supervised weights, and fix the docstring that claims a harness exists.**
`ic`/`ic_ir`/`regression`/`shapley`/`mutual_information` all fit and score the **same rows**
(`weighting.py:517/574/604/716` against `pipeline.py:228-235`); `shapley` explicitly maximises
in-sample rank correlation over subsets. Severity is currently limited — `cli.py` never passes
`forward_returns`, so every supervised method falls back to equal — but the API is exposed, and
`weighting.py:18-19` actively misleads a maintainer by stating these methods are testable "which is
why the bake-off harness exists". **No such harness exists in `src/` or `scripts/`.** Append
"weights fitted in-sample on the graded cross-section" to `ctx.warnings` absent an explicit
`oos=True`, and correct the docstring. Note `WEIGHTING.md §5.5` **already specifies** purged
forward-chaining CV in detail (6 blocks, calendar embargo, Newey-West lag bounds) — build that
spec when the time comes; do not author a competing one.

**W8.3 · Use the exact Euler decomposition for contributions.** `scoring.py:288-291` returns
`w_i·(score_i − 50)`, whose sum is exactly `weighted_mean − 50`, but `pipeline.py:409` applies it
to pillar scores that `pipeline.py:286-291` combined with **CES** — and all 11 profiles set
`pillar_aggregator='ces'`. Measured gaps on a realistic 8-pillar vector: **0.94** at rho=0.5,
**1.94** at rho=0.0 (the `value` profile), **3.01** at rho=−0.5 (`deep_value`). The report renders
these as the reason for the grade, so a user reconciling drivers against the headline finds up to
3 points unaccounted for. `test_invariants.py:273` asserts exactness only for `weighted_mean`, so
it passes today. Verified identity: `Σ s_i ∂M/∂s_i = M^(1−rho) · M^rho = M`.

```
ŵ_i = M^(1-rho) · w_i · s_i^(rho-1)        (geometric limit rho→0:  ŵ_i = M·w_i/s_i)
contribution_i = ŵ_i · (s_i - 50);   residual "_curvature" = 50·(1 - Σ ŵ_i)
```

Return `{}` for aggregators outside the family rather than a wrong number. Keep raw `w` in
`PillarScore.weights`; put `ŵ` in a new `effective_weights` field. `report.py:83-85` and `:238-242`
must skip the `_curvature` key.

**W8.4 · Three small documentation-vs-code corrections.** (a) `normalize.py:286-288`,
`pipeline.py:13-14` and `pipeline.py:436-438` all promise a fallback to "absolute piecewise
anchors" for a too-small universe. `MetricSpec` has **no `anchors` field at all**
(`registry.py:87` defines only `ideal_band`), so `pipeline.py:145` passes `anchors=None` because
nothing could ever supply them and `normalize.py:295`'s `usable < 2 and anchors` branch is
unreachable. Not currently producing wrong numbers (`pipeline.py:373-377` forces N/A for a
single-security universe), but three docstrings describe behaviour that does not exist. Either
build anchors or correct the text. (b) Add `provenance` / `citation` / `estimation_sample` to
`MetricSpec`, defaulting to `'derived'`, and fill it for Beneish (74 manipulators vs 2,332
controls, 1982-1992, threshold −1.78), Altman Z'' (33/33 manufacturers, 1946-1965) and Ohlson (105
bankrupt, 1970-1976) — sitting in a table beside 100 machine-generated ratios they read as
independent corroboration, with thresholds presented as physical constants. **Drop the companion
proposal to fence them out of supervised weighting on estimation-overlap grounds:** those samples
end in 1992/1965/1976 and the usable XBRL window starts 2012, so there is **zero** overlap.
Post-publication decay is the honest concern, and it is a documentation matter. (c) Report
**effective metric count** per pillar (eigen-entropy N from the Spearman matrix) in `explain` —
`annualized_return_1y`, `sharpe_ratio`, `sortino_ratio` and `calmar_ratio` are all `pillar='risk'`,
`direction=+1` and monotone in trailing return, so the `momentum` profile's momentum 0.50 + risk
0.20 at ρ=0.9 are not independent weights. Either reassign them as a versioned change or document
the overlap.

**W8.5 · Append the measured negatives to `DATA-GROUND-TRUTH.md` §1.** Its §1 records only Yahoo
and Stooq, which is what produced the incomplete "no free price source is reachable" conclusion.
Add: **api.nasdaq.com** — returns correct 10y OHLCV, but `robots.txt` is `User-agent: * /
Disallow: /`, so **measured and declined**; recording it as *declined* rather than *untested* is
what stops the next implementer shipping it because it happens to respond. (It also has no
adjusted-close column, which would reintroduce DO NOW 7 in reverse.) Add FRED `WILL5000IND` /
`WILLREITIND` (404, discontinued — do not code against them), the stockanalysis.com positive, and
a one-paragraph "measured, does not work" note on Benford's law (below). Date-stamp every row and
tag single-observation rows as the existing document already does.

---

# WORTH CONSIDERING

Real, but either low impact, contested benefit, or blocked on something that does not exist yet.
Revisit after DO NEXT.

**C1 · Peer-universe bootstrap for normalizer and rank estimation error.** `robust_z`'s median and
MAD, and `sector_neutral`'s within-group statistics at `min_group=5`, are all estimated from the
universe, and none of that error reaches the interval. Measured raw-composite sd from this source:
0.46 at N=200, 1.37 at N=15. Ranked here because DO NOW 9 already captures the **largest**
component (percentile instability) far more cheaply, leaving only the second-order term, and
because it needs `pipeline.py:221-293` refactored into a reusable `_composite_from_scores` first.
**If built: renaming duplicated index labels is mandatory, not cosmetic** — `out.loc[index] = ...`
in `sector_neutral` and the reindex in `_normalize` will silently corrupt under sampling with
replacement. Cache or skip the SLSQP weighting methods and record the omission.

**C2 · PIT-vs-LATEST as a calibration condition.** Grade the same universe at the same `asof`
twice and ask whether the PIT interval covers the LATEST score. `sec.py:170-183` already
implements both vintages, so this needs no new fetching. **This is the only genuinely external
truth available anywhere in this review** — every other calibration condition tests the model
against its own output. Verified on live data: 539 multi-vintage `(start, end)` pairs for AAPL
across 10 common tags, 5.2% revised, conditional median 16%, max 57%. Report it with and without
periods where the tag set changed. **Do not ship the accompanying empirical restatement prior
injected into every metric** — most large "revisions" are retrospective standard adoption (ASC 606,
ASU 2009-13), a definitional change rather than uncertainty about the past, so injecting them as
noise overstates error for a PIT grade.

**C3 · Shrink noisy time-series metrics toward neutral.** `robust_z` divides by the **observed**
cross-sectional MAD, so it is scale-free: a column that is pure estimation noise is stretched
across the full 0-100 range exactly like one that is pure signal. Pushing 82 statistically
identical simulated stocks through the repo's own `normalize_series` gave Sharpe scores spanning
**27.8 to 76.9** — a D-to-B+ spread manufactured from stocks identical by construction. Prefer
closed-form variances (`var(σ̂) = σ²/2T`, `var(SR_ann) = (1 + SR²/2)/n_years`, `var(ρ₁) = 1/T`,
Lo-MacKinlay `var(VR_q) = 2(2q−1)(q−1)/3qT`) over a stationary bootstrap. **Watch the λ=0 case:** a
flat-50 column must be renormalised **out** of its pillar, not counted as a full-weight average
vote — that is already-fixed bug #6 reappearing in a new place. Ranked here because 40 metrics are
`MISSING` today without prices, and because the specific 27%/98% reliability figures rest on
literature dispersions never measured in this environment.

**C4 · `DataQualityReport`, without the new `Coverage` state.** There is currently **no channel by
which a failed check can reach a score**: `SecuritySnapshot.warnings` is copied at
`pipeline.py:219` and printed, never scored, and `Coverage` has three states. Without this
plumbing, every check in W1 is invisible beyond a warning. Ship the reduced version — dataclasses,
a `report.py` section, and rejected values set to `NaN` (which already yields `MISSING` through the
existing path). **Hold back `Coverage.SUSPECT` and the multiplicative coverage penalty** until the
checks have a measured firing rate on a few hundred filers: folding "data present but wrong" into
the same number that gates grading at 0.35 means a filer could be refused a grade for a reason the
message misattributes. Note the full version also touches `engine.py:140-158` and
`pipeline.py:116/268/320-327/413`, which the original proposal omitted.

**C5 · Concept-scoped temporal jump bounds, warning-only.** A naive global rule is unusable: I
measured it firing on **2,353 of 22,586 windows (10.4%)**, dominated by `working_capital`, `cfi`
and `cff` — exactly the concepts the proposed VOLATILE bucket excludes, which validates the
remedy. Split into SMOOTH (robust z on log changes, threshold 6, 5% scale floor), LUMPY (sign only:
capex, buybacks, dividends, stock_issued) and VOLATILE (no bound). Ship warning-only and measure
per-concept firing rates before letting it drop values. **Lowest priority of the data-quality
work: it is the only proposal in the whole review with no named currently-wrong number attached.**

**C6 · Point-in-time SIC — measure before building.** `sec.py:474-478` assigns `snap.sector` from
**today's** SIC in both PIT and LATEST modes, and sector drives `is_applicable`,
`SECTOR_DISABLED_METRICS` (up to 30 metrics for a bank), `SECTOR_DISABLED_PILLARS` and
`altman_variant_for` — so a reclassified firm is graded historically under the metric set of the
business it is **today**, and coverage (which gates the refusal threshold) moves with it.
Unambiguous look-ahead, ranked last because the effect size is **entirely unmeasured**. First
measure how many universe CIKs cross a `_SIC_RANGES` boundary between their oldest cached filing
and today; if zero, ship only an assertion. If not, read the filing's **SGML header** via a Range
request on `https://www.sec.gov/Archives/edgar/data/{cik}/{accn}.txt`, which returns
`STANDARD INDUSTRIAL CLASSIFICATION: ... [3571]` (verified HTTP 200) — the originally-proposed
`-index-headers.html` URL **404s**, and `-index.htm` is a restylable page template.

**C7 · Make weighting fallbacks widen the interval instead of narrowing it.** Measured: with the
same score vector, equal weights produced a **narrower** interval than a skewed vector in 207/300
trials (mean −1.31 points of half-width) — so a run where the weighting method could not be
computed reports **more** confidence than one where it worked. Add a structured `fallbacks` list to
`WeightingContext` and inflate the weight-uncertainty variance by the worst severity. Ranked last
because reach is narrow (`uncertainty_interval` only ever sees pillar weights, and every profile
supplies fixed ones, so the pillar level never degrades) and it only becomes load-bearing after
W2.3. **The stated mechanism in the source finding is backwards** — total Dirichlet weight variance
is `(1 − Σw²)/(c+1)`, which is **maximised** at uniform (measured 0.01716 uniform vs 0.01506
skewed); the real cause is that an equal-weighted composite is less sensitive to reweighting.
Calibrate the severity constants from the harness rather than accepting the six guessed values.

**C8 · Fix `sector_neutral`'s silent NaN-sector drop (2 lines) — but leave the shrinkage alone for
now.** `values.groupby(sectors)` at `normalize.py:264` silently drops any ticker whose sector label
is NaN; those names keep the global score with no warning. That is a genuine 2-line bug worth
fixing regardless. The docstring at `:258` calls `n/(n + min_group)` "James-Stein-flavoured"; it is
not — real shrinkage depends on between-group signal over within-group noise, not group size. But
the proposed moment estimator will often produce `tau2 <= 0` in a small universe and **silently
collapse to global scoring**, i.e. switch sector-neutrality off without saying so. If shipped, warn
when `tau2` clips to zero. Low priority: `sector_neutral` is off by default (`pipeline.py:74`).

---

# DELIBERATELY NOT DOING

**This section is as valuable as the others.** Each of these is technically correct and was
argued for by a researcher. Not building them is a decision, not an oversight.

| # | Proposal | Why not |
|---|---|---|
| **N1** | **Circumvent Yahoo / Stooq bot detection** | Off limits regardless of technical feasibility. Yahoo returns 429 on every attempt; Stooq serves a JS proof-of-work challenge. Not a capability gap to engineer around. |
| **N2** | **Ship `api.nasdaq.com` as a price provider** | It returns correct 10-year OHLCV, but its `robots.txt` is `User-agent: * / Disallow: /`. **Measured and declined** — the verdict should not be softened because it happens to respond. It also has no adjusted-close column, which would reintroduce DO NOW 7 in reverse. Record it in `DATA-GROUND-TRUTH.md` so it is not rediscovered. |
| **N3** | **Full cross-source price validation with pairwise z-gating** | Correct in principle; the useful 90% collapses into two much cheaper checks that are already separate items (W4.5's magnitude guard and W4.3's provenance stamping). The full version needs `snapshot.insider_price` and `float_date` fields that do not exist, plus a per-security volatility estimate unavailable on exactly the scalar-price path it targets, plus a hand-tuned 0.30 annual-vol drift constant. Two of the three estimators are already reconciled in `sec_prices.py:407-456`. Revisit only if W4.1 lands a fourth dense source. |
| **N4** | **Replace MAD with Qn or the biweight midvariance** | Efficiency numbers are right (MAD 0.379 vs biweight 0.892 at n=50), but `robust_z` is an affine map plus a clip, so swapping the scale estimator is **rank-preserving** — it only moves who hits the clip. DO NOW 10 attacks that directly at a fraction of the cost: no O(n²) Qn, no `c=9` tuning constant, no movement in 246 golden values. *Steal one piece only:* the `MAD == 0` fallback to plain `zscore` (`normalize.py:101-103`), which falls back to the outlier-sensitive estimator `robust_z` exists to avoid — that 6-line swap is folded into DO NOW 10. |
| **N5** | **Cross-sectional winsorization / medcouple fence in `normalize_series`** | The premise is false (see dropped-WRONG #5): the weighting panel is the post-normalisation 0-100 matrix, so raw outliers never reach it. And the default `robust_z` already survives a 28.34 outlier intact — measured peer spread **42.16 preserved**, versus 1.59 for `minmax` and 0.89 for `zscore`. The fence would only help non-default normalizers while changing their documented semantics, at medium effort with an O(n²) medcouple and a very wide test blast radius. The salvageable half is W6.6. |
| **N6** | **Empirical-Bayes shrinkage of sparse pillar scores** | Its **negative** recommendation — do not impute, do not sector-median-fill — is valuable and is already the code's behaviour. The positive proposal needs universe-level variance components inside `build_pillar_score`, which sees one company at a time; its own measured λ is 0.42 even at **full** coverage, so shipping it would compress every pillar toward the mean and force a re-tune of `absolute_weight` and probably `GRADE_CUTOFFS`. Coverage is already surfaced honestly via `PillarScore.coverage`, `coverage_penalty` and `MIN_COVERAGE_TO_GRADE`. Large change, contested benefit, one maintainer. |
| **N7** | **Lo autocorrelation and c4 small-sample corrections to Sharpe/Sortino/Calmar** | All three points are technically correct and none pays. c4 is **+0.3% at n=252** and is a uniform cross-sectional scale factor, so it changes no rank. Lo's correction needs daily continuity that does not exist here. The Mertens standard error has **no consumer** — nothing downstream of `MetricResult.raw_inputs` reads a precision, so it would be computed and discarded. Revisit Lo alone if a real daily series lands. |
| **N8** | **Convergent/discriminant validity tests between profiles as pytest assertions** | The observation is a **symptom of the dead-pillar problem (DO NOW 11), not an independent defect**, and the thresholds are too unstable to encode. On a 30-name panel `momentum` vs `low_volatility` measured **0.589 — passing** the proposed 0.60 ceiling, while on the source's 18-name panel it failed at 0.820. Rank correlation at n=18-30 carries SE ≈ 0.18-0.24, so the suite would flake on which companies happen to be cached, and a green run would **falsely certify** discriminant validity the system does not have. Keep it as a diagnostic script if at all. |
| **N9** | **Leave-one-out weighting to enforce strict monotonicity** | Discards the explicit architectural invariant at `pipeline.py:221-223` ("the weights describe the metric set, not any individual security"), costs *n* weight computations per pillar, and makes `PillarScore.weights` per-ticker — to remove a **sub-1% violation** (entropy 5/600, worst drop 1.14 pts; pca 1/600) of a property nobody promised, on opt-in methods only. Ship the metamorphic **test** with a documented rate instead (W7.4). |
| **N10** | **Segment / dimensional data from the DERA financial-statement data sets** | The substrate is real (2025q1.zip returns HTTP 200) but the deliverable is a 1-6 GB local mirror, a build script, a new module and a quarterly refresh chore — for **three** quality-pillar metrics whose segment members are filer-defined free text. Only shape statistics (count, HHI, dispersion) are cross-company comparable, reorganisations manufacture spurious dispersion, and PIT correctness is coarser than the `companyfacts` path. Disproportionate for one maintainer. |
| **N11** | **Benford's law digit analysis** | **Measured, does not work at single-company scale.** All ten sampled filers reject at p < 2e-5 — MCD χ²=78.6, WMT 222.3, JPM 182.9, TGT 96.6 — including companies with no detected defect. Do not build even the cross-sectional variant: the identity checks in DO NOW 3/4 and W1.6 have measured false-positive rates near zero and strictly dominate it. Record as a negative in `DATA-GROUND-TRUTH.md`. *(Two other verified negatives worth recording: `companyfacts` carries **0 dimensional records out of 270,508**, so segment contamination is a non-issue; and `_quarters_spanned` misclassified **0 of 164,445** duration records, so 52/53-week calendars are safe.)* |
| **N12** | **CSCV / probability-of-backtest-overfitting** | `C(16,8) = 12,870` recombinations, built **before** the walk-forward harness that would give it something to split. Its own author correctly recommended against it. Likewise `deflated_sharpe`, whose `var_sr_across_trials` would be guessed rather than measured — decorative. Build W7.5's two pure functions now; revisit after `WEIGHTING.md §5.5` is implemented. |
| **N13** | **Make `hrp` the default metric weighting** | Rejected on three grounds. `robust_z` already equalises each column's cross-sectional dispersion, so HRP's variance budgeting has little left to allocate and degenerates toward correlation clustering; HRP on a ~82-row × 11-column score panel is badly conditioned (the proposal's own Marchenko-Pastur caveat concedes it); and `equal` is the **zero-estimation-error null** that the multiple-testing work in W7.5 depends on. Adopt the effective-metric-count *reporting* (W8.4c); reject the default change. |
| **N14** | **Make `gaussian_rank` the default normalizer** | It would make the **absolute** half of the hybrid curve rank-based too (`absolute_weight = 0.5`), removing the only defence against a uniformly-bad universe manufacturing an A. `robust_z` preserves within-universe magnitude — a name 5 MADs above median should score far above one at 0.5 MAD. Take the adaptive clip (DO NOW 10) instead. |
| **N15** | **Logit reparameterisation of the interval bounds** | Solves a problem that does not occur: **0 clipped bounds in 500 adversarial trials** at scores 80-99 with coverage 0.5. It would churn every existing test fixture to fix nothing. The off-centre half of that finding **is** real and is taken (W2.4). |
| **N16** | **D&A-rate and interest-vs-debt cross-statement bands** | Miscalibrated as specified. The D&A band `[0.02, 0.45]` gives **3/10 false positives** (MCD 0.013, ABBV 0.008, BLK 0.004) and only ABBV is the intended catch; the interest band `[0.005, 0.25]` fires on non-banks **TGT (0.858)** and XOM (0.303), so the proposed BANK/INSURANCE skip does not save it — and it was justified using interest/**revenue** ratios while proposed on interest/**debt**. Ship only check 1 (W1.6), or re-derive the bands first. |
| **N17** | **Q4 filed-date vintage guard** | Does not fire on its own motivating example. Target's winning FY record was filed 2015-03-13 / 2016-03-11, **after** all four quarters (2013-05-30 … 2014-11-26), so "skip if any quarter was filed after the FY record" never triggers. Use the value-conflict test (W1.5) instead. |
| **N18** | **Equity-veto for split detection** | A stock split does not move `StockholdersEquity` **at all**, so the veto never fires and cannot discriminate. Use the EPS corroboration (W1.2). |
| **N19** | **Peer-relative ratio bounds as an imputation source** | The coverage-QA half is taken (W7.2); the imputation half is **one careless call away from becoming the point imputation that caused already-fixed bug #6**, and the uncertainty machinery is not currently shaped to accept per-metric interval widening. |
| **N20** | **Raise the Beneish component gate from 6 to 8** | Costs coverage (26 → 18 computed on a real panel) without touching the actual mechanism. The TATA loss-maker gate (W6.3) is the fix. Also drop the claim that Beneish/Altman/Ohlson estimation samples overlap the backtest window — they end 1992/1965/1976 and the XBRL window starts 2012, so overlap is **zero**. |

---

# CREATIVE WORKAROUNDS

Things that unlock capability currently believed blocked. Confidence is stated honestly.

### 1. Daily split- and dividend-adjusted OHLCV, keyless — `stockanalysis.com`

**Unlocks:** 40 of 105 metrics that are unconditionally dead today (risk 24, momentum 13,
liquidity 3), plus a real market cap for the valuation pillar in place of the sparse insider-price
crutch, plus a total-return benchmark leg for workaround 2.
**Confidence: tested-works** *(single observation, 2026-07-24)*.

Measured: `GET https://stockanalysis.com/api/symbol/s/AAPL/history?range=10Y&period=Daily` →
HTTP 200, 252,977 bytes, **2,513 rows**, newest bar `{'t':'2026-07-24','c':333.02,'a':333.02}`.
Dotted tickers and ETFs work (`BRK.B`, `SPY`). Unknown symbols return **HTTP 400**, not an empty
frame. `robots.txt` is `User-agent: *` / `Disallow:` with only dotbot/BLEXBot/mj12bot blocked. No
key, no cookie, no bot-check.

The **self-proving test** is what makes this trustworthy: `BRK.B` has `a == c` on every bar (never
paid a dividend), while `T` shows `a << c` (2016 bar: c=42.38, a=17.80) and `SPY` likewise
(c=216.75, a=184.50). Ship it as `assert |cagr(a) − cagr(c)| < 0.001` for BRK.B and `> 0.05` for T.

Implementation notes, all measured: rows come **newest-first**, which is only safe because
`_conform` sorts (`prices.py:67`) — do not remove that. Handle only the **bare-list** `data` shape
(the only one observed); keep a defensive `isinstance` check but log loudly on anything else.
`range=10Y` is anchored to **today** with no start/end params, so `--asof` before ~2016 silently
yields an empty frame after `PriceProvider.get` truncates — fail soft, never extrapolate. The `ch`
column is dropped harmlessly by `_conform`'s column projection.

**Honest caveat the maintainer owns, not me:** this is an **undocumented private endpoint of a
commercial site**. It is permitted by `robots.txt` as measured today and it is not a bot-detection
bypass, but it is not a licensed feed either, it can change or disappear without notice, and its
Terms of Service are a judgement call that should be read before shipping. Keep it behind the
existing fail-soft `PriceProvider.get` contract, keep `CSVPriceProvider` as the guaranteed path,
and record it in `DATA-GROUND-TRUTH.md` with its measurement date.

### 2. A real benchmark series, so three dead metrics can fire — FRED (+ SPY from workaround 1)

**Unlocks:** `beta`, `capm_alpha`, `idiosyncratic_volatility` — declared (`types.py:197`), consumed
(`engine.py:67`), required by three metrics, and **assigned by nothing outside the test suite**.
They can never fire in any configuration today, and they additionally drag every security's
coverage down because `engine.py:67-70` marks them `MISSING` while `METRICS.md:151,163,164` says
they should be `NOT_APPLICABLE`.
**Confidence: tested-works.**

Measured: `fredgraph.csv?id=SP500` → HTTP 200 (2016-07-25 … 2026-07-24); `NASDAQCOM` → HTTP 200
**back to 1971-02-05** (so the "FRED is 10y only" caveat applies to SP500 **alone**); `DJIA` and
`VIXCLS` → HTTP 200. `WILL5000IND` and `WILLREITIND` → **404, discontinued — do not code against
them.** Coerce the `.` holiday rows with `pd.to_numeric(errors='coerce')`.

Add `--benchmark` (default SPY). Prefer SPY `adj_close` from workaround 1 — a genuine
**total-return** leg. Fall back to FRED over the existing `RiskFreeProvider` plumbing (the tool
already talks to that host, no key). Stamp `meta['benchmark_is_price_only'] = True` on the FRED
path so the reader knows alpha is inflated by roughly `beta × index dividend yield` (~1.8pp/yr).
Reconcile the `MISSING` vs `NOT_APPLICABLE` discrepancy while there.

### 3. SEC XBRL frames as an offline oracle and a survivorship-free universe

**Unlocks:** (a) universe-wide coverage QA — the mechanism that found the `liabilities` gap;
(b) an **independent** check on `normalize_duration_facts`, where four of the seven known bugs
lived and which is currently tested only against self-authored fixtures; (c) a **CIK-keyed
point-in-time universe**, the only real fix for survivorship.
**Confidence: tested-works.**

Measured: `frames/us-gaap/Revenues/USD/CY2024Q1` → HTTP 200, 319,680 bytes;
`GrossProfit/USD/CY2024` → 2,613 companies; `RevenueFromContractWithCustomerExcludingAssessedTax/
USD/CY2024Q1` → 2,654 filers with AAPL at `90,753,000,000`. Seven tag frames covering 6,628 filers
cost **7 requests and ~5.7 MB**, versus roughly 30 GB and hours to assemble the same cross-section
from `companyfacts`. Dead issuers are fully retrievable: CIK 106455 (Westmoreland Coal, bankrupt
2018) returns 1,881,697 bytes and 9 dated `EntityPublicFloat` records.

Widening the cross-section is also the **single most effective lever against the multiple-testing
problem**: simulated `E[max IC | null]` at 105 metrics falls from **0.306 at N=82 to 0.122 at
N=500**. Caveat: frames are calendar-aligned, so 52/53-week filers are absent and coverage figures
are a **lower bound**; compare against a stored baseline, never an absolute. Prices for delisted
names remain unobtainable, so a full-universe run stays fundamentals-only.

### 4. Real outcome labels without forward returns — already shipping, with one refinement

**Unlocks:** the ability to ask "do these grades separate anything?" — which the project has
already answered once, catching the `accruals_ratio` inversion (AUC 0.29 against going-concern
filers) and fixing it with a positive-earnings gate.
**Confidence: tested-works (in production).**

`validation.py:46-54` `LABEL_QUERIES` already carries **bankruptcy, restatement, going_concern and
material_weakness**, all exposed via `scripts/validate_distress.py --label`. The restatement query
is the literal Item 4.02 title; going-concern uses the ASU 2014-15 phrase.

The genuinely new refinement: switch the collector from EDGAR full-text phrase matching to the
**structured `items` field** in `data.sec.gov/submissions/CIK*.json` — verified that filtering on
`"1.03"` (bankruptcy) and `"4.02"` (non-reliance) works cleanly. More precise, avoids the
10,000-result cap, needs no negation filtering. The remaining work is W7.1: making it offline and
CI-guarded, and size/SIC-matching the controls.

*(One correction to a widely-repeated date: BBBY's first Item 1.03 8-K is **2023-04-24**, not
2023-09-20, which changes the `asof` lead time a harness should use.)*

### 5. Bank and REIT metrics from XBRL already on disk — no new data source at all

**Unlocks:** the sector coverage gap. Measured against the live registry: of **65 price-free**
metrics, GENERAL gets 65, **BANK gets 36**, REIT 52, INSURANCE 47. `sectors.py:112` disables the
entire **efficiency** pillar for banks (0 of 6), and only **3 of 12** price-free health metrics
survive. A three-metric pillar carries several times the sampling variance of a twelve-metric one,
so bank grades are *uninformative* rather than wrong.
**Confidence: tested-works for input availability; likely for the metrics themselves.**

**Banks** — the inputs are sitting unused in payloads already cached. JPM carries
`InterestIncomeExpenseNet` (232 USD facts), `NoninterestExpense` (232), `NoninterestIncome` (232),
`Deposits` (144), `ProvisionForLoanLeaseAndOtherLosses` (232),
`FinancingReceivableAllowanceForCreditLoss...` (76); WFC carries the same NII/NIE/NonII trio at 224
each. Add `efficiency_ratio`, `net_interest_income_to_assets`, `fee_income_share`,
`deposits_to_assets`, `loans_to_deposits`, `allowance_coverage`, `provision_burden`,
`tangible_common_equity_ratio`. **Two honest limitations to preserve:** there is no earning-asset
tag, so call it `net_interest_income_to_assets`, **not NIM**; and Tier-1/CET1 are untagged, so TCE
is a proxy. Register every new metric in `SECTOR_DISABLED_METRICS` for all non-bank classes so no
industrial takes a coverage penalty, and list `deposits`/`loans`/`allowance` in `concepts.py`'s
`_INSTANT` set or they will be TTM-**summed**. Rather than re-enabling the efficiency pillar for a
single metric, place `efficiency_ratio`, `fee_income_share` and `provision_burden` there together.

**REITs** — verified negative first, which saves a week: scanning **every** taxonomy (us-gaap, dei
and company extensions) across all 147 cached payloads, including five REITs, yields **zero**
`FundsFromOperation`, `SameStore`, `SameProperty` or occupancy tags. Any design reading FFO from
XBRL would be 100% `MISSING`. But the reconstruction inputs are all present — Realty Income carries
`DepreciationDepletionAndAmortization` (233), `ImpairmentOfRealEstate` (186),
`GainLossOnSaleOfProperties`, `RealEstateInvestmentPropertyAtCost` (126) and
`...AccumulatedDepreciation` (134):

```
ffo = net_income_to_common + real_estate_depreciation - gain_on_sale + impairment
```

Treat the two **adjustments** as zero-when-absent and the two **core** items as required. Add
`price_to_ffo`, `ffo_payout_ratio`, `ffo_cagr_3y`, `property_noi_margin`, `real_estate_age`. Reject
outside `1.0 <= ffo / net_income_to_common <= 6.0`. **Say plainly in the report that this is a
reconstruction, not the company's published figure.** Chain order matters:
`GainsLossesOnSalesOfInvestmentRealEstate` is **absent** from O; `GainLossOnSaleOfProperties` is
the tag actually present. *(One correction to the source finding: 12 of 16 valuation metrics are
**kept** for REITs, not disabled — only 4 are dropped. Thinner, not gutted.)*

**Revenue must be defined per business model before any of this counts.** Measured on the real
code, bank revenue is **already non-comparable inside the peer group**: AXP resolves to
`RevenueFromContractWithCustomerExcludingAssessedTax` at $43.1B TTM (contract revenue only) while
JPM / WFC / C / BAC all resolve to `Revenues` at $95.1B / $74.3B / $85.2B / $115.1B on materially
different bases. So `price_to_sales`, `ev_to_sales`, `net_margin` and `asset_turnover` are ranking
banks on incompatible measurements **today**. Route revenue through a `SECTOR_REVENUE` table
(BANK = NII + noninterest income, requiring **both** components — `min_count = len(parts)`, never
1), remove `RevenuesNetOfInterestExpense` and `InterestAndDividendIncomeOperating` from the GENERAL
chain (which also removes the verified AXP +357% seam), and record `meta['revenue_basis']`. This
changes every historical grade for financials — regenerate stored backtests.

### 6. `dei:EntityCommonStockSharesOutstanding` as a free independent cross-check

**Unlocks:** detection of us-gaap share-count scale errors in **one comparison**, at zero network
cost, where the frames-quantile approach measurably fails (dropped-WRONG #2).
**Confidence: tested-works.** MCD's `ttm('shares_diluted')` is 716.3 while the DEI tag reads
710,505,859 on the same date — ratio 9.96e5. The test is one line:
`reject when |log10(ttm_shares / dei_shares)| > 0.5`. Detail in W1.3.

### 7. PIT-vs-LATEST as free external truth

**Unlocks:** the only calibration condition in this entire review that tests the model against
something other than its own output. `sec.py:170-183` already implements both vintages, so it costs
no new fetching logic. **Confidence: likely** (mechanism verified; the interpretation caveat about
retrospective standard adoption is real). Detail in C2.

---

# What cannot be verified in this environment

Stated plainly, because several recommendations above rest on numbers I could not re-measure.

1. **No forward returns exist, and none can be obtained.** Everything requiring a return time
   series is unbuildable *and* unmeasurable here: the information coefficient of any metric, the
   Sharpe of the grader itself, deflated Sharpe, CSCV/PBO, Hansen SPA, and any claim that a
   weighting method is *better* rather than merely *different*. The supervised weighting methods
   (`ic`, `ic_ir`, `regression`, `shapley`, `mutual_information`) are reachable only from a
   programmatic call — `cli.py` never passes `forward_returns` — so their defects are latent API
   hazards, correctly ranked below live ones.
2. **Price data is the binding constraint, and the workaround is a single observation.** Yahoo
   (429) and Stooq (JS challenge) are blocked; circumventing them is off limits. The
   stockanalysis.com endpoint was measured **once**, on 2026-07-24, and has no SLA, no
   documentation and no contract. Treat every price-dependent item as conditional on it. Keyed
   providers (tiingo, FMP, alphavantage, polygon, finnhub) remain untested — nobody has signed up.
3. **Numbers I did not personally reproduce**, carried on the auditors' word and flagged where
   they matter: the HubSpot/PCA public-float scale-error rate (0.7%); FINRA RegSHO volume
   fractions, the MIDAS 403 and the bulk 13F 404s; the Broadcom 10:1 split figures; the exact
   `liabilities` recovery count (**750 vs 788** — the two audits disagreed and I could not settle
   it offline); the claimed 3.9% runtime overhead of the contract checks.
4. **Two audits disagreed and I chose a side.** Recorded so the choice is auditable: the
   `LiabilitiesAndStockholdersEquity` question (DO NOW 4 — I sided with *derive, do not put it in
   the chain*, because the 99.42% equal-to-Assets measurement is decisive), and the default
   universe size (**82 tickers**, verified myself; the "~24 names" claim came from counting file
   lines and inflated one recommendation's arithmetic by ~2×).
5. **Sector-specific reconstructions are unvalidated against ground truth.** The REIT FFO formula
   reproduces NAREIT's *definition*, but no published FFO figure exists in XBRL to check it
   against — that is precisely why it must be reconstructed. Expect the `[1.0, 6.0]` sanity band to
   need calibration on real filers, and label the output as a reconstruction in the report.
6. **Calibration targets are self-referential until C2 lands.** The T1 harness (W2.1) measures the
   interval against *simulated* truth, so it can prove the interval is mis-calibrated (it already
   does: 0.697 → 0.395 against an advertised 0.90) but it cannot prove the grade is *right*.
   PIT-vs-LATEST is the only external check available.
7. **Effect sizes I flagged as unmeasured**, and which should be measured before the corresponding
   work is funded: SIC drift across `_SIC_RANGES` boundaries (C6 — possibly zero); the coverage
   cost of DO NOW 2's staleness bound against `MIN_COVERAGE_TO_GRADE = 0.35`; and the per-concept
   firing rates for C5's temporal bounds.

---

# Sequencing

The dependency structure matters more than the ordering within tiers.

```
DO NOW 2  (latest() age bound)  ──┐
                                  ├──►  W1.1 (per-period resolution + splice gate)
DO NOW 4  (derive liabilities) ───┘        └──►  W1.5 (restatements) ──► strong form of DO NOW 6

DO NOW 9  (hybrid interval)  ◄──── MUST ship with ──►  W2.1 (calibration harness)
                                                          └──► W2.2, W2.3, W2.4, W2.5

W3.1 (sparse gate) ──► W3.3 (pairwise-complete) ──► W3.2 (Ledoit-Wolf)   [order matters]

Workaround 1 (prices) ──► Workaround 2 (benchmark) ──► W4.4, W6.1, W6.5 become live rather than latent
```

Three sequencing rules worth stating explicitly:

- **Ship DO NOW 2 before W1.1.** The staleness bound converts the Lowe's-class wrong numbers into
  `MISSING` in an afternoon; the per-period resolution fix that *actually* recovers the data is a
  week of careful work including ~40 hand-assigned equivalence classes. Take the safety net first.
  That is what de-urgents the expensive fix.
- **Never ship DO NOW 9 without W2.1.** It widens the interval; W2.5b narrows it. Without the
  harness you cannot tell whether the combination improved coverage or made it worse.
- **Expect coverage to fall.** DO NOW 1, 2, 5, 6 and 8 all convert confident wrong numbers into
  honest `MISSING`. Measure how many of the 82 default names cross below
  `MIN_COVERAGE_TO_GRADE = 0.35` **before** merging, and be willing to move that threshold rather
  than weaken a check. A refusal to grade is a correct output; a plausible wrong grade is not.
