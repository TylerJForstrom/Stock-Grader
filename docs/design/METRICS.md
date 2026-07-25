# METRICS.md — final deduplicated metric catalog

**Status:** normative. This file, `SPEC.md` and `WEIGHTING.md` are the merged build contract.
`docs/design/DATA-GROUND-TRUTH.md` overrides all three on data-source questions.

**Counts:** 89 metrics across 10 pillars. Derived from the 102 currently registered in
`src/stock_grader/metrics/{fundamental,statistical}.py` by dropping 15 duplicates and adding 2
(`ohlson_o_score`, `beneish_m_score`). §5 lists every drop with its reason.

---

## 1. Dedup rule (why some correlated metrics survive and others do not)

A metric is **removed** only when it is one of:

1. an **exact reciprocal** of another metric (Spearman |ρ| = 1 on the common support);
2. a **deterministic function of other catalog members** in the same pillar (a linear combination,
   a threshold, a unit conversion);
3. **strictly dominated** — measures the same quantity with a strictly worse estimator;
4. **misfiled** into a pillar whose question it does not answer, while a correct-pillar equivalent
   already exists.

Merely *correlated* metrics are kept. Redundancy among them is a modelling problem owned by the
weighting layer (`critic`, `hrp`, `choquet_2additive`, `dedup_corr`), not a reason to delete
information. Every kept metric therefore carries a `redundancy_group` (§4); metrics sharing a group
are expected to correlate above ~0.8 and are the intended input to those mechanisms.

**Direction convention** — `+1` higher-is-better, `-1` lower-is-better, `0` non-monotonic ideal band.
`direction == 0` ⟺ `shape == "band"` is an assertion enforced at registration (SPEC §3.4); `0` is the
*only* encoding of a band, and a band metric may never be routed to a monotonic normalizer.

**Sign-flip rule (applied throughout valuation).** Any ratio whose denominator can change sign is
registered in **yield form** (small denominator → large magnitude → *worse*, not "cheap"). Ranking
raw `P/E` puts loss-makers at the cheap end, which is backwards; `earnings_yield` puts them at the
expensive end, which is correct. This is why `pe_trailing`, `price_to_fcf`, `price_to_ocf`, and the
four `ev_to_*` profit multiples do not appear below in multiple form.

**Notation.** `ttm(x)` = trailing-twelve-month value of concept `x` (`Fundamentals.ttm`, returns
`None` unless four contiguous quarters exist). `latest(x)` = most recent reported instant.
`EV` = market_cap + total_debt + preferred_equity + minority_interest − cash − short_term_investments.
`safe_div(a, b, positive_denominator=True)` returns `None` when `b <= 0` or either side is non-finite.
`MCAP` = price × shares_outstanding. `r_t` = daily log return. `TD` = 252 trading days.

---

## 2. The catalog

### 2.1 Valuation (13)

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `earnings_yield` | valuation | +1 | `ttm(net_income) / MCAP` | net_income, price, shares_outstanding | `MCAP <= 0` → None. Negative numerator is **kept** (a loss-maker legitimately scores at the expensive end). Replaces `pe_trailing`. |
| `fcf_yield` | valuation | +1 | `ttm(fcf) / MCAP`, `fcf = cfo − \|capex\|` | cfo, capex, MCAP | Any component None → None. Negative kept. Replaces `price_to_fcf`. |
| `ocf_yield` | valuation | +1 | `ttm(cfo) / MCAP` | cfo, MCAP | Negative kept. Replaces `price_to_ocf`. |
| `ebitda_to_ev` | valuation | +1 | `ttm(ebitda) / EV`, `ebitda = ebit + D&A` | operating_income, D&A, EV components | `EV <= 0` → None (net-cash company: multiple is meaningless, yield would be misleadingly huge). Negative EBITDA kept. |
| `ebit_to_ev` | valuation | +1 | `ttm(ebit) / EV`, `ebit := operating_income`, fallback `pretax_income + interest_expense` | operating_income \| (pretax_income, interest_expense), EV | `EV <= 0` → None. Absorbs the former `acquirers_multiple`, which computed the identical quantity. |
| `fcf_to_ev` | valuation | +1 | `ttm(fcf) / EV` | cfo, capex, EV | `EV <= 0` → None. |
| `gross_profit_to_ev` | valuation | +1 | `ttm(gross_profit) / EV` | gross_profit, EV | `EV <= 0` → None. `NOT_APPLICABLE` for sectors with `has_cogs == False` (banks, insurers, REITs, holdings). |
| `ev_to_sales` | valuation | −1 | `EV / ttm(revenue)` | EV, revenue | Kept in multiple form: revenue is never negative, so the sign-flip rule does not apply. `revenue <= 0` → None; `EV <= 0` → None. Winsor cap 500×. |
| `price_to_sales` | valuation | −1 | `MCAP / ttm(revenue)` | MCAP, revenue | `revenue <= 0` → None. Cap 500×. Differs from `ev_to_sales` only by leverage; same redundancy group. |
| `price_to_book` | valuation | −1 | `MCAP / latest(equity)` | MCAP, equity | `equity <= 0` → None (negative book value is not "cheap"). Cap 500×. |
| `price_to_tangible_book` | valuation | −1 | `MCAP / (equity − goodwill − intangibles)` | MCAP, equity, goodwill, intangibles | Tangible book `<= 0` → None. Missing goodwill/intangibles treated as 0 only if `assets` is present, else None. Cap 500×. |
| `peg_ratio` | valuation | −1 | `(1 / earnings_yield) / (100 × g)`, `g` = 3y annual net-income CAGR | net_income (4 annual), MCAP | `earnings_yield <= 0` → None; `g <= 0` → None (a PEG with negative growth is a sign error, not a bargain). Cap 20. Uses `earnings_yield` since `pe_trailing` was removed. |
| `graham_number_ratio` | valuation | +1 | `sqrt(22.5 × eps_ttm × bvps) / price` | net_income, equity, shares_outstanding, price | `eps <= 0` or `equity <= 0` → None (the square root is undefined and Graham's screen excludes loss-makers by construction). |

### 2.2 Profitability (11)

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `gross_margin` | profitability | +1 | `ttm(gross_profit) / ttm(revenue)` | gross_profit, revenue | `revenue <= 0` → None. `NOT_APPLICABLE` where `has_cogs == False`. |
| `operating_margin` | profitability | +1 | `ttm(operating_income) / ttm(revenue)` | operating_income, revenue | `revenue <= 0` → None. Negative numerator kept. |
| `net_margin` | profitability | +1 | `ttm(net_income) / ttm(revenue)` | net_income, revenue | `revenue <= 0` → None. Negative kept. |
| `ebitda_margin` | profitability | +1 | `ttm(ebitda) / ttm(revenue)` | ebitda, revenue | `revenue <= 0` → None. EBITDA composed at the TTM level from `ebit` and `D&A` separately — never by summing a per-period derived column (differing missingness produces EBITDA below EBIT). |
| `fcf_margin` | profitability | +1 | `ttm(fcf) / ttm(revenue)` | cfo, capex, revenue | `revenue <= 0` → None. Renamed from `free_cash_flow_margin`. |
| `roe` | profitability | +1 | `ttm(net_income) / avg(latest 2 annual equity)` | net_income, equity | `avg equity <= 0` → None. Kept despite leverage contamination; `roic`/`croic` are the unlevered readings. |
| `roa` | profitability | +1 | `ttm(net_income) / avg(latest 2 annual assets)` | net_income, assets | `assets <= 0` → None. |
| `roic` | profitability | +1 | `ebit × (1 − tax_rate) / latest(invested_capital)`, `invested_capital = total_debt + equity − cash` | operating_income, income_tax, pretax_income, debt, equity, cash | `invested_capital <= 0` → None. `tax_rate = clip(income_tax / pretax_income, 0, 0.5)`; `pretax_income <= 0` → use 0.21 statutory default and note it. |
| `croic` | profitability | +1 | `ttm(fcf) / latest(invested_capital)` | cfo, capex, invested_capital | `invested_capital <= 0` → None. |
| `gross_profit_to_assets` | profitability | +1 | `ttm(gross_profit) / latest(assets)` | gross_profit, assets | `assets <= 0` → None. `NOT_APPLICABLE` where `has_cogs == False`. |
| `margin_trend` | profitability | +1 | OLS t-statistic of the slope of `operating_margin` on time, over the last 5 annual periods | operating_income, revenue (5 annual) | Fewer than 4 usable periods → None. Zero residual variance → None (not ±inf). Winsor ±10. |

### 2.3 Growth (9)

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `revenue_cagr_3y` | growth | +1 | `(rev_t / rev_{t−3})^(1/3) − 1` | revenue (4 annual) | `rev_{t−3} <= 0` → None. Winsor [−1, 3]. |
| `revenue_cagr_5y` | growth | +1 | `(rev_t / rev_{t−5})^(1/5) − 1` | revenue (6 annual) | As above. Same redundancy group as the 3y form. |
| `earnings_cagr_3y` | growth | +1 | `(ni_t / ni_{t−3})^(1/3) − 1` | net_income (4 annual) | Base or end `<= 0` → None. A CAGR across a sign change is arithmetically undefined, not "infinite growth". |
| `earnings_cagr_5y` | growth | +1 | as above over 5y | net_income (6 annual) | As above. |
| `fcf_cagr_3y` | growth | +1 | `(fcf_t / fcf_{t−3})^(1/3) − 1` | cfo, capex (4 annual) | Base or end `<= 0` → None. |
| `book_value_cagr_5y` | growth | +1 | `(bv_t / bv_{t−5})^(1/5) − 1` | equity (6 annual) | Base or end `<= 0` → None. |
| `revenue_growth_consistency` | growth | +1 | `R²` of `ln(revenue)` regressed on time, 5 annual periods | revenue (5 annual) | Any non-positive revenue → None. Fewer than 4 points → None. Output already in [0, 1]. |
| `earnings_growth_consistency` | growth | +1 | fraction of the last 5 annual periods with `Δ net_income > 0` | net_income (6 annual) | Fewer than 4 deltas → None. Ordinal with heavy ties → `normalizer_override: quantile_bucket(B=5)`. |
| `revenue_growth_acceleration` | growth | +1 | `revenue_cagr_3y − revenue_cagr_5y` (difference of the two CAGRs, in ratio units) | revenue (6 annual) | Either component None → None. Winsor [−2, 2]. Deterministically derived from two catalog members → `composite: true` (excluded from correlation/PCA panels). |

### 2.4 Financial health (11)

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `current_ratio` | health | +1 | `current_assets / current_liabilities` | current_assets, current_liabilities | Denominator `<= 0` → None. `NOT_APPLICABLE` where `has_classified_balance_sheet == False` (banks, insurers, REITs, holdings) — this is *not* missing data and must not be penalised. Cap 20. |
| `quick_ratio` | health | +1 | `(current_assets − inventory) / current_liabilities` | current_assets, inventory, current_liabilities | Missing `inventory` → treat as 0 **only** if `current_assets` present; else None. Same `NOT_APPLICABLE` rule. Cap 20. |
| `cash_ratio` | health | +1 | `(cash + short_term_investments) / current_liabilities` | cash, sti, current_liabilities | Missing `sti` → 0. Same `NOT_APPLICABLE` rule. Cap 20. |
| `debt_to_equity` | health | −1 | `(long_term_debt + short_term_debt) / latest(equity)` | debt, equity | `equity <= 0` → None (a negative ratio is not low leverage). Cap 20. A `capital_structure_band` variant (`direction = 0`, band 0.2–0.8) is available via `config/normalize.yaml` for profiles that treat zero leverage as suboptimal; **default stays −1** because within a *health* pillar the question is solvency, not optimality. |
| `debt_to_assets` | health | −1 | `total_debt / latest(assets)` | debt, assets | `assets <= 0` → None. |
| `net_debt_to_ebitda` | health | −1 | `(total_debt − cash − sti) / ttm(ebitda)` | debt, cash, sti, ebitda | `ebitda <= 0` → None (an unpayable ratio, not a good one). Net cash gives a legitimate negative → kept. Winsor [−10, 30]. |
| `interest_coverage` | health | +1 | `ttm(ebit) / \|ttm(interest_expense)\|` | ebit, interest_expense | `interest_expense == 0` (debt-free) → return the winsor cap 100 and set `note="no_interest_expense"`, **not** None: debt-free is the best possible coverage and dropping it would penalise the safest names. Winsor [−10, 100]. |
| `fcf_to_debt` | health | +1 | `ttm(fcf) / total_debt` | cfo, capex, debt | `total_debt == 0` → cap 10 with `note="no_debt"` (same reasoning as above). Winsor [−5, 10]. |
| `altman_z` | health | +1 | `1.2·(WC/A) + 1.4·(RE/A) + 3.3·(EBIT/A) + 0.6·(MCAP/TL) + 1.0·(Sales/A)` | working_capital, retained_earnings, ebit, MCAP, liabilities, revenue, assets | Any component None or `assets <= 0` or `liabilities <= 0` → None. **Original manufacturing Z only** — the Z′ and Z″ variants have different coefficients *and* different cutoffs, so they are separate metrics if ever added, never a silent substitution. `composite: true`. `normalizer_override: piecewise_linear_absolute` with anchors at the published cutoffs 1.81 (distress) / 2.99 (safe); the cutoffs are the signal, so cross-sectional ranking is forbidden. `NOT_APPLICABLE` for banks/insurers/REITs. Winsor [−10, 20]. |
| `ohlson_o_score` | health | −1 | Ohlson (1980) 9-term logit score; report the score, not the probability | assets, liabilities, working_capital, current_liabilities, current_assets, net_income (2y), cfo, GNP deflator (constant) | Any component None → None. **New metric.** `composite: true`; `normalizer_override: piecewise_linear_absolute` anchored at the published 0.38 probability cutoff (score 0.5). Direction −1 (higher O ⇒ higher bankruptcy probability). `NOT_APPLICABLE` for financials. |
| `piotroski_f_score` | health | +1 | count of 9 binary tests (profitability 4, leverage/liquidity 3, efficiency 2) | net_income, cfo, assets, debt, current ratio components, shares_diluted, gross_margin, asset_turnover — all across 2 annual periods | Any of the 9 sub-tests unavailable → None for the whole score (a 7-of-9 F-score is not comparable to a 9-of-9 one). **Integer 0–9 ordinal with massive ties**: `normalizer_override: piecewise_linear_absolute` with anchors at the nine integers (or `quantile_bucket(B=9)`). Cross-sectional z / MAD is meaningless here — MAD is frequently exactly 0. `composite: true`. |

### 2.5 Efficiency (5)

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `asset_turnover` | efficiency | +1 | `ttm(revenue) / avg(latest 2 annual assets)` | revenue, assets | `assets <= 0` → None. |
| `days_sales_outstanding` | efficiency | −1 | `365 × latest(receivables) / ttm(revenue)` | receivables, revenue | `revenue <= 0` → None. Winsor [0, 365]. |
| `days_inventory_outstanding` | efficiency | −1 | `365 × latest(inventory) / ttm(cogs)` | inventory, cogs | `cogs <= 0` → None. `NOT_APPLICABLE` where `has_cogs == False`. Winsor [0, 730]. Absorbs the former `inventory_turnover`, its exact reciprocal ×365. |
| `capex_intensity` | efficiency | **0** | `\|ttm(capex)\| / ttm(revenue)`; ideal band **[0.02, 0.10]** | capex, revenue | `revenue <= 0` → None. **Direction changed from −1 to 0.** Zero capex is underinvestment, not efficiency; both tails are worse than the middle. Scored by `double_sigmoid`; a monotonic normalizer on this metric is rejected by the config validator. |
| `rnd_intensity` | efficiency | +1 | `ttm(rnd_expense) / ttm(revenue)` | rnd_expense, revenue | Missing `rnd_expense` → `NOT_APPLICABLE`, **not** 0. A utility that reports no R&D line has not spent zero on R&D in a comparable sense; zero-filling drags every non-tech name to the bottom of the distribution. |

### 2.6 Earnings quality (6)

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `accruals_ratio` | quality | −1 | `(ttm(net_income) − ttm(cfo)) / avg(latest 2 annual assets)` | net_income, cfo, assets | `assets <= 0` → None. Winsor [−2, 2]. |
| `cash_conversion` | quality | +1 | `ttm(cfo) / ttm(net_income)` | cfo, net_income | `net_income <= 0` → None (the ratio flips sign meaninglessly). Winsor [−5, 5]. |
| `fcf_to_net_income` | quality | +1 | `ttm(fcf) / ttm(net_income)` | cfo, capex, net_income | `net_income <= 0` → None. Winsor [−5, 5]. Differs from `cash_conversion` only by capex; same redundancy group. |
| `goodwill_to_assets` | quality | −1 | `latest(goodwill) / latest(assets)` | goodwill, assets | Missing `goodwill` → **0.0** with `note="no_goodwill_reported"` (unlike R&D, a company with no goodwill line genuinely has no goodwill). `assets <= 0` → None. |
| `share_count_change` | quality | −1 | 3y CAGR of annual `shares_diluted` | shares_diluted (4 annual) | Base `<= 0` → None. Winsor [−0.5, 0.5]. Distinct from `buyback_yield`: share count also moves on SBC and stock-funded M&A. |
| `beneish_m_score` | quality | −1 | Beneish (1999) 8-variable M-score: `−4.84 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI + 4.679·TATA − 0.327·LVGI` | receivables, revenue, gross_profit, assets, ppe_net, securities, D&A, sganda_expense, net_income, cfo, debt — each across 2 annual periods | Any of the 8 indices unavailable → None. **New metric.** `composite: true`. `normalizer_override: piecewise_linear_absolute` anchored at the published −1.78 manipulation threshold. Direction −1 (higher M ⇒ more likely manipulated). Requires two consecutive annual periods with contiguous coverage. |

### 2.7 Shareholder return (4)

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `dividend_yield` | shareholder | +1 | `\|ttm(dividends_paid)\| / MCAP` | dividends_paid, MCAP | Missing `dividends_paid` → **0.0** with `note="no_dividend"` — a non-payer has a real zero yield, and the cross-section is expected to be >50% ties (which is precisely why `robust_z`'s MAD-zero → IQR fallback is mandatory). Winsor [0, 0.3]. |
| `payout_ratio` | shareholder | **0** | `\|ttm(dividends_paid)\| / ttm(net_income)`; ideal band **[0.25, 0.60]** | dividends_paid, net_income | `net_income <= 0` → None. Non-payer → 0.0, which the band correctly scores low-but-not-worst. Winsor [0, 3]. Band metric; monotonic normalizers rejected. |
| `fcf_payout_ratio` | shareholder | **0** | `\|ttm(dividends_paid)\| / ttm(fcf)`; ideal band **[0.20, 0.60]** | dividends_paid, cfo, capex | `fcf <= 0` → None. Same band handling. Same redundancy group as `payout_ratio`. |
| `buyback_yield` | shareholder | +1 | `(\|ttm(buybacks)\| − \|ttm(stock_issued)\|) / MCAP` | buybacks, stock_issued, MCAP | Missing `buybacks` → **0.0**; missing `stock_issued` → 0.0 only when `buybacks` is present. Winsor [−0.5, 0.5]. Genuinely negative for net issuers. |

> `shareholder_yield` (removed) was `dividend_yield + buyback_yield` **exactly** — a perfect linear
> dependence that gave dividends double weight under any linear aggregator. See §5.

### 2.8 Risk (17) — all `needs_prices=True`

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `annualized_volatility` | risk | −1 | `std(r_t, ddof=1) × sqrt(252)` over 252d | adj_close | `< 126` observations → None. Zero variance (halted/illiquid) → None, not 0. |
| `downside_deviation` | risk | −1 | `sqrt(mean(min(r_t − mar, 0)²)) × sqrt(252)`, `mar = 0` | adj_close | `< 126` obs → None. Fewer than 5 negative days → None. |
| `idiosyncratic_volatility` | risk | −1 | annualised std of residuals from `r_t = α + β·r_mkt,t + ε_t` | adj_close, benchmark | Needs benchmark; absent → `NOT_APPLICABLE`, not missing. `< 252` overlapping obs → None. |
| `volatility_of_volatility` | risk | −1 | std of a rolling 21-day realised-vol series over 378d | adj_close | `< 378` obs → None. |
| `max_drawdown` | risk | −1 | `min_t(P_t / cummax(P)_t − 1)`, reported as a positive magnitude | adj_close | `< 252` obs → None. Always ≤ 0 before the sign convention; report `\|·\|` so direction −1 reads naturally. |
| `ulcer_index` | risk | −1 | `sqrt(mean(D_t²))`, `D_t = 100 × (P_t/cummax(P)_t − 1)` | adj_close | `< 252` obs → None. Captures drawdown *duration*, which `max_drawdown` does not; same redundancy group. |
| `cvar_95` | risk | −1 | mean of the worst 5% of daily returns, annualised by `× sqrt(252)`, reported as a positive magnitude | adj_close | `< 252` obs → None. Replaces `var_95`: CVaR is coherent and VaR is not, and CVaR conditions on the same tail. |
| `return_skew` | risk | +1 | sample skewness of `r_t` over 252d, `bias=False` | adj_close | `< 252` obs → None. Zero variance → None. Winsor ±5. |
| `excess_kurtosis` | risk | −1 | sample excess kurtosis of `r_t` over 252d, Fisher | adj_close | `< 252` obs → None. Winsor [−5, 30]. |
| `tail_ratio` | risk | +1 | `\|q95(r)\| / \|q05(r)\|` | adj_close | `q05 == 0` → None. Winsor [0, 5]. |
| `hill_tail_index` | risk | +1 | Hill estimator of the left-tail index on the worst `k = floor(0.05n)` losses | adj_close | `< 504` obs or `k < 10` → None. Higher index ⇒ thinner tail ⇒ better. Winsor [0, 10]. |
| `sharpe_ratio` | risk | +1 | `mean(r_t − rf_t) / std(r_t − rf_t) × sqrt(252)` | adj_close, risk_free | `< 252` obs → None; `std <= 0` → None. Missing `risk_free` → use 0 and record `warning="no_risk_free_rate"`. Winsor ±5. |
| `sortino_ratio` | risk | +1 | `mean(r_t − rf_t) / downside_deviation_daily × sqrt(252)` | adj_close, risk_free | `< 252` obs, or fewer than 5 negative excess days → None. Winsor [−5, 10]. |
| `calmar_ratio` | risk | +1 | `annualised_return_3y / max_drawdown_3y` | adj_close | `< 756` obs → None; `max_drawdown == 0` → None. Winsor ±10. |
| `capm_alpha` | risk | +1 | annualised intercept `α` of `r_t − rf_t = α + β(r_mkt,t − rf_t) + ε_t` | adj_close, benchmark, risk_free | No benchmark → `NOT_APPLICABLE`. `< 252` overlapping obs → None. Winsor ±2. |
| `beta` | risk | **0** | `cov(r, r_mkt) / var(r_mkt)`; ideal band **[0.60, 1.20]** | adj_close, benchmark | No benchmark → `NOT_APPLICABLE`. `var(r_mkt) <= 0` → None. Band widened from the registered (0.70, 1.15) and re-centred on 1.0: the band encodes "market-like exposure is neutral", and a profile wanting monotone low-beta preference sets `direction_override: -1` in its profile config (SPEC §9.3). Winsor [−3, 5]. |
| `variance_ratio` | risk | **0** | `Var(r^(q)) / (q · Var(r^(1)))` at `q = 5`, Lo–MacKinlay; ideal band **[0.90, 1.10]** | adj_close | `< 504` obs → None. The **single canonical serial-dependence metric**; `hurst_exponent`, `return_autocorrelation` and `mean_reversion_half_life` all measured the same property and were removed (§5). |

### 2.9 Momentum (10) — all `needs_prices=True`

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `short_term_reversal_1m` | momentum | **−1** | 21-day total return | adj_close | `< 63` obs → None. **Direction flipped from +1 and renamed** from `momentum_1m`: the codebase's own `momentum_12_1` docstring states the most recent month exhibits *reversal*, so registering it as a positive momentum signal contradicted the file's own finance. Winsor [−1, 3]. |
| `momentum_6m` | momentum | +1 | 126-day total return | adj_close | `< 147` obs → None. Winsor [−1, 5]. |
| `momentum_12_1` | momentum | +1 | 231-day total return skipping the most recent 21 days | adj_close | `< 273` obs → None. The skip is load-bearing — it removes the reversal month now captured separately. Winsor [−1, 5]. |
| `risk_adjusted_momentum` | momentum | +1 | `momentum_12_1 / annualized_volatility` | adj_close | Either component None, or volatility `<= 0` → None. `composite: true` — a deterministic function of two catalog members in two different pillars; kept because it encodes an interaction a linear aggregator cannot express, but excluded from correlation/PCA panels and from the Choquet prior. Winsor ±10. |
| `momentum_consistency` | momentum | +1 | fraction of the last 12 months with positive monthly return | adj_close | `< 273` obs → None. Ordinal-ish (13 levels) → prefer `quantile_bucket(B=6)` or `winsor_rank`. |
| `information_discreteness` | momentum | −1 | `sign(PRET) × (%neg_days − %pos_days)` over the 12-1 window (Da–Gurun–Warachka) | adj_close | `< 273` obs → None. Winsor ±1. Lower = smoother information flow = stronger continuation. |
| `distance_from_52w_high` | momentum | +1 | `P_t / max(P over 252d)` | adj_close | `< 200` obs → None. Bounded (0, 1]; near-1 predicts continuation. |
| `price_to_sma200` | momentum | +1 | `P_t / mean(P over last 200d)` | adj_close | `< 200` obs → None; `sma <= 0` → None. Winsor [0, 3]. |
| `trend_strength` | momentum | +1 | t-statistic of the slope of `ln(P)` regressed on time over 252d | adj_close | `< 252` obs → None; zero residual variance → None. Winsor ±50. |
| `rsi_14` | momentum | **0** | Wilder 14-day RSI; ideal band **[40, 65]** | adj_close | `< 40` obs → None. All-gain window (`loss == 0`) → return 100.0, which the band correctly scores as stretched rather than best. Band metric; monotonic normalizers rejected. |

### 2.10 Liquidity (3) — all `needs_prices=True`

| name | pillar | direction | formula | inputs | edge-case rule |
|---|---|---|---|---|---|
| `dollar_volume` | liquidity | +1 | `median(close × volume)` over 63d | close, volume | `< 63` obs or missing `volume` → None. **`size_neutral_ok: false`** — this metric *is* a size proxy; residualising it on log market cap destroys the signal and the validator blocks it. Normalized in log space (`x_space: log`). |
| `amihud_illiquidity` | liquidity | −1 | `mean(\|r_t\| / (close_t × volume_t)) × 1e6` over 126d | adj_close, close, volume | `< 126` obs → None. Days with `volume == 0` are **excluded from the mean**, not treated as infinite illiquidity. `size_neutral_ok: false`. |
| `zero_return_days` | liquidity | −1 | fraction of the last 126 days with `r_t == 0` | adj_close | `< 126` obs → None. Heavy ties at 0 for liquid names → MAD is frequently 0; the IQR fallback in `robust_z` is mandatory. |

---

## 3. Pillars

| pillar | n | needs prices | default level-2 weight (`all_weather`) |
|---|---|---|---|
| valuation | 13 | no | 0.18 |
| profitability | 11 | no | 0.18 |
| health | 11 | no | 0.16 |
| growth | 9 | no | 0.14 |
| quality | 6 | no | 0.14 |
| momentum | 10 | **yes** | 0.08 |
| shareholder | 4 | no | 0.07 |
| efficiency | 5 | no | 0.05 |
| risk | 17 | **yes** | 0.00 (profile-specific) |
| liquidity | 3 | **yes** | 0.00 (profile-specific) |

30 of 89 metrics require prices. Per `DATA-GROUND-TRUTH.md`, no free price source is reachable
from the build environment, so **the fundamentals-only path is the default path**: when
`snapshot.has_prices` is False the `risk`, `momentum` and `liquidity` pillars are marked
`NOT_APPLICABLE` in their entirety and are removed from the *coverage denominator*, not counted as
missing (SPEC §5.2). Without that rule the corrected `c_top` formula would drive every offline
grade to `NR`.

---

## 4. Registry flags (per-metric, beyond the six columns above)

Set via the extended `@metric` decorator (SPEC §3.4). Defaults: `shape` derived from `direction`,
`size_neutral_ok=True`, `composite=False`, `redundancy_group=None`, `normalizer_override=None`.

| flag | value | metrics |
|---|---|---|
| `shape: band` (⟺ `direction == 0`) | — | `capex_intensity`, `payout_ratio`, `fcf_payout_ratio`, `beta`, `variance_ratio`, `rsi_14` |
| `composite: true` (excluded from correlation / PCA / Choquet-prior panels) | — | `altman_z`, `ohlson_o_score`, `piotroski_f_score`, `beneish_m_score`, `risk_adjusted_momentum`, `revenue_growth_acceleration` |
| `size_neutral_ok: false` | — | `dollar_volume`, `amihud_illiquidity` |
| `normalizer_override` | `piecewise_linear_absolute` | `altman_z`, `ohlson_o_score`, `beneish_m_score` |
| `normalizer_override` | `piecewise_linear_absolute` (9 integer anchors) | `piotroski_f_score` |
| `normalizer_override` | `quantile_bucket(B=5)` | `earnings_growth_consistency` |
| `x_space: log` | — | `dollar_volume`, `ev_to_sales`, `price_to_sales`, `price_to_book`, `price_to_tangible_book`, `peg_ratio` |
| `NOT_APPLICABLE` by sector class | no classified balance sheet | `current_ratio`, `quick_ratio`, `cash_ratio` |
| `NOT_APPLICABLE` by sector class | no COGS | `gross_margin`, `gross_profit_to_assets`, `gross_profit_to_ev`, `days_inventory_outstanding` |
| `NOT_APPLICABLE` by sector class | financials | `altman_z`, `ohlson_o_score` |
| `NOT_APPLICABLE` when input absent | no benchmark | `beta`, `capm_alpha`, `idiosyncratic_volatility` |
| `NOT_APPLICABLE` when input absent | no R&D line | `rnd_intensity` |

**Redundancy groups** (expected |ρ| > 0.8; the input to `dedup_corr`, `critic` and the Choquet prior):

| group | members |
|---|---|
| `val_sales` | `ev_to_sales`, `price_to_sales` |
| `val_book` | `price_to_book`, `price_to_tangible_book` |
| `val_cashflow` | `fcf_yield`, `ocf_yield`, `fcf_to_ev` |
| `val_earnings` | `earnings_yield`, `ebit_to_ev`, `ebitda_to_ev` |
| `prof_returns` | `roe`, `roa`, `roic`, `croic` |
| `prof_margins` | `gross_margin`, `operating_margin`, `net_margin`, `ebitda_margin`, `fcf_margin` |
| `growth_revenue` | `revenue_cagr_3y`, `revenue_cagr_5y` |
| `growth_earnings` | `earnings_cagr_3y`, `earnings_cagr_5y` |
| `health_liquidity` | `current_ratio`, `quick_ratio`, `cash_ratio` |
| `health_leverage` | `debt_to_equity`, `debt_to_assets`, `net_debt_to_ebitda` |
| `health_distress` | `altman_z`, `ohlson_o_score` |
| `qual_cash` | `cash_conversion`, `fcf_to_net_income`, `accruals_ratio` |
| `share_payout` | `payout_ratio`, `fcf_payout_ratio` |
| `risk_vol` | `annualized_volatility`, `downside_deviation`, `idiosyncratic_volatility` |
| `risk_drawdown` | `max_drawdown`, `ulcer_index`, `calmar_ratio` |
| `risk_ratio` | `sharpe_ratio`, `sortino_ratio` |
| `risk_tail` | `cvar_95`, `tail_ratio`, `hill_tail_index`, `excess_kurtosis` |
| `mom_trend` | `price_to_sma200`, `distance_from_52w_high`, `trend_strength` |
| `mom_return` | `momentum_6m`, `momentum_12_1`, `risk_adjusted_momentum` |

---

## 5. Removals, renames and direction changes (audit trail)

### 5.1 Removed (15)

| removed | rule | reason |
|---|---|---|
| `pe_trailing` | 1 | Exact reciprocal of `earnings_yield`. Its negative-denominator behaviour also placed loss-makers at the "cheap" end of a rank. |
| `price_to_fcf` | 1 | Exact reciprocal of `fcf_yield`. |
| `acquirers_multiple` | 2 | `EV / operating_income`, and `ebit := operating_income` (`data/sec.py:416`) — so it is *literally* `ev_to_ebit`, now `ebit_to_ev`. |
| `inventory_turnover` | 1 | `cogs / inventory` is the exact reciprocal of `days_inventory_outstanding / 365`. |
| `shareholder_yield` | 2 | `dividend_yield + buyback_yield` exactly (`fundamental.py:676`). Perfect collinearity; under any linear aggregator it doubles the weight of dividends. Components retained — they carry different signals. |
| `annualized_return_1y` | 4 | Filed under `risk` but it is a return, not a risk measure; dominated by `momentum_12_1`, which correctly skips the reversal month. |
| `var_95` | 3 | Dominated by `cvar_95`: same tail, coherent risk measure, and CVaR is not blind to tail shape beyond the quantile. |
| `cornish_fisher_var` | 2 | A deterministic function of `var_95`, `return_skew` and `excess_kurtosis` — all separately in the catalog. Keeping it makes the Choquet interaction prior circular by construction. |
| `atr_percent` | 3 | A third total-volatility estimator, ρ ≈ 0.95 with `annualized_volatility`, and it additionally requires `high`/`low` columns the offline fixtures may not carry. |
| `hurst_exponent` | 2 | Measures the same serial dependence as `variance_ratio` (H > 0.5 ⟺ VR > 1), with a noisier estimator and a 504-day requirement. |
| `return_autocorrelation` | 2 | The lag-1 special case of `variance_ratio`. |
| `mean_reversion_half_life` | 2 | `ln(0.5)/ln(1 + φ)` where φ is the AR(1) coefficient — a deterministic function of `return_autocorrelation`, itself removed. |
| `momentum_3m` | 2 | Nested inside `momentum_6m` and `momentum_12_1`; the most reversal-contaminated of the three and the least standard. |
| `golden_cross` | 3 | A *binary threshold* on the same 50/200-day information as `price_to_sma200`. Binary columns have Bernoulli variance, MAD exactly 0, and near-total ties — they silently break `robust_z`, every dispersion weighting method and `quantile_bucket`. |
| `pct_positive_days` | 2 | Daily-frequency restatement of `momentum_consistency`, which is measured at the monthly horizon that matches the momentum window. |

### 5.2 Renamed (8)

| old | new | why |
|---|---|---|
| `price_to_ocf` | `ocf_yield` | Sign-flip rule; direction −1 → +1. |
| `ev_to_ebitda` | `ebitda_to_ev` | Sign-flip rule; direction −1 → +1. |
| `ev_to_ebit` | `ebit_to_ev` | Sign-flip rule; direction −1 → +1. |
| `ev_to_fcf` | `fcf_to_ev` | Sign-flip rule; direction −1 → +1. |
| `ev_to_gross_profit` | `gross_profit_to_ev` | Sign-flip rule; direction −1 → +1. |
| `graham_number` | `graham_number_ratio` | The registered name promised a dollar figure; the function returns a ratio to price. Name now matches the unit. |
| `free_cash_flow_margin` | `fcf_margin` | Consistency with every other `fcf_*` name (and with the function's own identifier). |
| `momentum_1m` | `short_term_reversal_1m` | The name asserted a momentum signal; the quantity is a reversal signal. Direction +1 → −1. |

All eight old names are registered as **deprecated aliases** that resolve to the new metric and emit
`DeprecationWarning` (SPEC §12.1), so existing YAML and CLI flags keep working for one minor version.

### 5.3 Direction changes (3)

| metric | old | new | why |
|---|---|---|---|
| `capex_intensity` | −1 | **0** (band 0.02–0.10) | The normalization spec's own `double_sigmoid` entry names capex intensity as a band metric while the registry had it monotone. Zero capex is underinvestment. |
| `short_term_reversal_1m` | +1 | **−1** | See §5.2. |
| the five reciprocal-ised valuation ratios | −1 | **+1** | Mechanical consequence of the sign-flip rule; the underlying preference is unchanged. |

### 5.4 Added (2)

`ohlson_o_score` (health, −1) and `beneish_m_score` (quality, −1). Both critiques required an
explicit normalization rule for the four bundled accounting composites (Piotroski, Altman, Ohlson,
Beneish); two of the four were absent from the registry entirely. Both are `composite: true` with
`piecewise_linear_absolute` anchored at their published thresholds, and both are excluded from every
correlation-, PCA- and Choquet-derived structure because they are built from ratios that also appear
standalone in the same pillars.

### 5.5 Known open item

`beta`'s band encoding is a genuine judgement call: within a `low_volatility` profile, monotone
lower-is-better is arguably correct, while within `all_weather` a band around 1.0 is. The catalog
ships the band and exposes `direction_override` per profile rather than forking the metric, because
a metric's identity should not depend on who is reading it. Flagged for empirical settlement by
`src/stock_grader/eval/` once forward returns are available.
