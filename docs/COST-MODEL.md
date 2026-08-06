# Per-row trading costs

The evaluator used to charge one number — a flat rate in basis points — to every
name in every period. That is a defensible approximation for the largest, most
liquid names and a badly wrong one everywhere else, and the direction of the
error matters more than its size: **a flat rate undercharges thinly traded names
and overcharges liquid ones**. Any comparison of a signal's performance across
liquidity tiers therefore has a thumb on the scale in favour of the thin tier,
which is precisely the comparison a "small caps are less efficient" claim rests
on. An edge that survives a flat charge and dies at a realistic one is not an
edge.

This document describes the replacement. It is methodology only: no measured
value from any private archive appears here, and none may be added
(`tests/test_licensing_wall.py` enforces that).

## Where the pieces live

| piece | module |
|---|---|
| the model — estimators, composition, refusals | `stock_grader/costs.py` |
| the cross-repository pin | `stock_grader/config/cost_golden_vectors.json` |
| per-row cost written onto the evaluable panel | `stock_grader/signal_panel.py` |
| per-row cost charged in the net spread | `stock_grader/backtest.py` |

Stock-Vault implements the same model independently — the two repositories may
not import each other — and the golden-vector file is what keeps them from
drifting. Both carry byte-identical copies and both assert the sha256 of its
**canonical content** (`json.dumps(payload, sort_keys=True,
separators=(",", ":"))`), not of its raw bytes: git rewrites line endings on
checkout, so a byte hash of a text file fails on a Windows runner for a reason
that has nothing to do with the cost model, and a cross-repository agreement
that breaks on a checkout setting is not an agreement. `.gitattributes`
separately keeps the stored bytes at LF; that is tidiness, the canonical hash
is the guarantee.

The file pins the model at **two** levels. `vectors` pins the composition —
liquidity inputs in, one cost estimate out — and takes `cs_spread_bps` as a
scalar *input*, so on its own it says nothing whatsoever about the four
estimators that produce that input: two implementations could disagree about
the overnight-gap adjustment, the flooring rule or the usable-pair definition
and still reproduce every composition vector. `bar_vectors` pins the estimators
themselves — raw highs/lows/closes/volumes in, spread/sigma/lambda out —
including what the same tape produces *without* the gap adjustment, so an
implementation that drops it fails the pin instead of agreeing on a narrower
spread. That blind spot was not hypothetical: it hid exactly that divergence
between the two repositories until 2026-08-05.

## Composition

All quantities are in basis points of price. `Q$` is the position notional.

```
spread_bps    = max( CS21 , 10^4 * 0.01 / price )        full spread
half_bps      = spread_bps / 2

Q$_allowed    = min( Q$ , 0.01 * ADV20$ )                participation cap
u             = Q$_allowed / ADV20$

g             = 0.314 * sigma * u ** 0.891               permanent impact
h             = 0.142 * sigma * u ** 0.600               temporary impact
athl_bps      = 10^4 * ( g / 2 + h )
amihud_bps    = lambda * Q$_allowed / 10^6               lambda in bps per $1M
impact_bps    = max( athl_bps , amihud_bps )

one_way_bps   = half_bps + impact_bps
round_trip    = 2 * one_way_bps
fill_buy      = price * (1 + one_way_bps / 10^4)
fill_sell     = price * (1 - one_way_bps / 10^4)
```

`sigma` is the standard deviation of daily log close-to-close returns over the
same 21-session window as the spread estimate. `ADV20$` is the median of
`close * volume` over the trailing 20 archived sessions — median, because a
single volume spike should not reprice a name, and dollars rather than shares.

### The spread term

Corwin & Schultz (2012), high-low, with their overnight-gap adjustment, over a
21-session window. Three properties of the implementation are load-bearing:

**The overnight-gap adjustment is applied, and the estimator takes closes to
apply it.** When the previous session's close sits outside the current
session's range, the whole range is shifted so it just touches that close;
otherwise an overnight move is read as intraday range, which is read in turn as
spread. The omission is not neutral: a gap inflates the two-day span `gamma`
far more than it inflates the sum of the one-day ranges `beta`, which shrinks
`alpha` and shrinks the estimate — a one-sided *understatement* of spread,
largest in exactly the gappy thin names this model exists to price. On the
pinned `gapped-window` vector the adjusted estimate is 128.9 bps and the
unadjusted one floors at zero. `corwin_schultz_spread_bps` therefore requires
`closes`; there is no signature that silently returns the unadjusted number.

**Negative estimates are floored once, on the window mean.** A large minority of
two-day estimates come out negative on real data. Flooring each *pair* at zero
before averaging is a truncation bias whose size scales with volatility rather
than with spread; applied to a real cross-section it inflates the most liquid
tier's estimate by more than an order of magnitude and inverts the liquidity
gradient entirely. Corwin & Schultz discuss both treatments; only the
window-mean floor is admissible here.

**A one-cent tick is a hard floor.** You cannot cross a spread narrower than one
tick. This is an institutional bound, not a fitted parameter, and it binds
hardest in cheap names.

Stated plainly, because it would otherwise be read the wrong way: **the spread
term is not the source of separation between liquidity tiers, and a range-based
estimator cannot be.** Names that barely trade have small high-low ranges, and a
range-based estimator reads "no range" as "no spread" — the opposite of the
truth. The spread term supplies a floor-level charge. The capacity economics
come from the impact term.

An alternative, Abdi & Ranaldo (2017), was implemented and rejected on a
mega-cap anchor where the true effective spread is known from public
TAQ-based research to be a few basis points: it produced estimates wrong by one
to two orders of magnitude for several of the most heavily traded securities on
the tape. A third, Fong, Holden & Trzcinka (2017), collapses to identically zero
above any reasonable liquidity screen, because a literal zero daily return
essentially never occurs under sub-penny US quoting.

### The impact term

Almgren, Thum, Hauptmann & Li (2005), "Direct Estimation of Equity Market
Impact", at their published calibration, for an order worked over one full
trading session at a constant rate (`T = 1`). The `g / 2` is the paper's own
accounting for a complete execution: half the permanent impact plus all of the
temporary impact.

**The participation assumption is stated rather than hidden.** The whole order
is worked over one session, so `u` is both the fraction of ADV and the
participation rate. It is the same session the simulator fills in, so the cost
and the fill are internally consistent. It is optimistic about patience: a real
one-day workup carries timing risk this model does not charge.

**The Amihud floor is a conservatism rule declared in advance, not a fitted
blend.** The ATHL calibration comes from a large/mid-cap institutional sample
and may understate the thin tail, so the impact term is floored by the archive's
own Amihud (2002) coefficient over the same window. Taking the larger of two
documented estimators can only overstate cost. That asymmetry is the correct
one: overstating cost cannot manufacture an edge, understating it can.

### The participation cap

No simulated order may exceed 1% of the name's `ADV20$`. This is conservative
against the 10–20% participation-of-volume convention, deliberately: a capacity
claim measured at an aggressive participation rate is a claim about the model,
not about the market.

Truncation is **counted, reported, and applied**
(`capacity_truncated_rows`, `capacity_truncated_notional_fraction`). A tier
whose orders are mostly refused has no edge to measure at that size, whatever
its cost column says about the fraction that did fit.

Applied matters, and for a while it was not. `estimate_cost` caps the notional
at 1% of `ADV20$` and prices the **capped slice**, so the returned
`round_trip_bps` is the cost of the notional that fit, not of the notional that
was asked for. If the evaluator then holds the name at a full equal weight, the
capacity constraint has been converted into a discount: the row is charged what
$20k of a $2M-ADV name costs while contributing the return of $100k of it, and
the thinnest band — the one the whole small-cap programme exists to
interrogate — is flattered by construction. Worse, once the cap binds,
participation is pinned at exactly `max_participation` for every name, so the
ATHL term stops varying with ADV at all.

The evaluator therefore weights every row by
`cost_notional_allowed_usd / cost_notional_target_usd`: a name the cap could
fill 21% of holds 21% of an equal-weight position and contributes 21% of the
exposure, and its cost is charged on those dollars. Both the return and the
cost of a leg are then per **deployed** dollar and describe the same position.
`BacktestReport.mean_deployable_fraction` states how much of the intended book
could actually be carried, the markdown prints it, and a run below 1.0 carries
a limitation saying so — because a spread of X% on 21% of the intended capital
is not the same claim as a spread of X%. Reading a band whose cap binds
therefore needs either that weighting or a re-price at a smaller notional; the
raw inputs ride along on the panel so the second is a recomputation.

## The flat model is a degenerate case, and that is a test

With `CS = 0`, a price whose one-cent tick is exactly a 10 bps full spread,
`sigma = 0`, `lambda = 0` and no participation cap, the composition returns a
5 bps one-way charge, a 10 bps round trip, and a fill at `P * (1 ± 5e-4)` — the
historical flat model, exactly. That is `GOLDEN_VECTORS[0]`, and
`tests/test_costs.py` asserts it. A change that breaks it has moved every number
ever recorded under the flat assumption.

## Refusals

Three, and all three produce nothing rather than something plausible:

* **Short window.** A name-date whose window holds fewer than 11 usable
  consecutive-session pairs yields no estimate. Below roughly half a window the
  estimators are reading stale prints, and a stale print reads as "no range",
  which reads as "no spread" — an optimistic fabrication in exactly the names
  that can least afford one.
* **Missing inputs.** All five liquidity inputs, or none. A cost assembled from
  four measured inputs and one substituted default is worse than no cost at all,
  because it reads exactly like a measured one.
* **Split contamination.** Consecutive-session pairs spanning a confirmed split
  are excluded from all three estimators together and counted. A split is not
  noise in this context, it is corruption: it reads as a large close-to-close
  return and as an overnight gap the Corwin-Schultz adjustment will "correct" by
  shifting a whole session's range. Excluding it from `sigma` but not from the
  spread would leave the two terms describing different tapes.

A refused row carries a null cost, is counted in `no_cost_estimate_rows`, and is
dropped from the evaluated cross-section — never back-filled from a tier average
and never charged a default.

## How the panel and the evaluator use it

The v6 return join reads the five liquidity inputs from the vault's observation
part, prices each row at the entry close (the price the fill actually happens
at) and at a declared per-position notional, and writes:

`round_trip_cost_bps`, `one_way_cost_bps`, `half_spread_bps`, `impact_bps`,
`cost_participation`, `cost_capacity_truncated`, `cost_notional_target_usd`,
`cost_notional_allowed_usd`, `cost_model_id`, plus the raw inputs
(`cost_adv20_dollar`, `cost_sigma`, `cost_cs_spread_bps`,
`cost_amihud_lambda_bps_per_musd`).

The raw inputs ride along so that re-pricing at a different deployable size is a
recomputation over the existing panel rather than a rebuild of it — panel parts
are immutable, and a capacity ladder would otherwise need one namespace per
rung.

`build.json` records the model id, the notional, the coverage, the truncation
counts and the golden-vector sha256. A net number whose cost model and position
size are not on the artifact is not reproducible: the same panel priced at two
sizes is two different measurements wearing the same column name.

The evaluator charges each quantile leg the mean cost of the names actually in
that leg, applied to that leg's turnover — the same shape the flat charge
always had, with a per-leg rate instead of a global constant. On a panel that
carries the capacity columns the leg mean is weighted by deployable notional
(above), so the rate is what the leg pays per dollar it could actually hold.
When the panel carries no cost column, the original single-constant expression
runs unchanged and the numbers are bit-identical to what they were before this
existed; `tests/test_backtest.py` pins that against literals taken from the
pre-change implementation.

The report gains `per_row_costs_used`, `mean_round_trip_cost_bps`,
`mean_rank_ic_net_side_aware`, `no_cost_estimate_rows`, `capacity_weighted` and
`mean_deployable_fraction`. The cost-net rank IC is reported only when costs
actually vary by name: subtracting one constant from every name is a monotone
transform, so a flat-cost "net IC" is the gross IC wearing a different label.

**The cost-net rank IC is side-aware, and that is not a refinement.** A cost is
a loss on whichever side you take it, so the long half is ranked against
`r - c` and the short half against `r + c`; the middle buckets are held on
neither side and pay nothing. Subtracting `c` from *every* name is the net
return of a long-only book, and because expensive names cluster at low scores
it pushes the short leg's ranked returns further down and mechanically RAISES
the correlation. Measured on real banded panels the single-signed statistic
came out larger than the *gross* IC on runs whose net spread was negative: two
cost-net numbers in one report table pointing opposite ways, with the more
flattering one carrying the more impressive name. The side-aware form can only
attenuate. `mean_rank_ic_net` was renamed to `mean_rank_ic_net_side_aware` so
that a consumer of the old, differently-defined statistic fails loudly instead
of reading the new one as if nothing had changed.

## What this model does not capture

Every one of these is a real cost that a result built on this model silently
omits. They are listed here so that no reader has to discover them by being
wrong later.

* **No quote data.** There is no NBBO; the spread is never observed, and its
  stand-in is provably blind to liquidity in exactly the tail the model exists
  to price.
* **No intraday data at all.** Fills are at the session close: no timing risk,
  no VWAP slippage, no open/close auction dynamics, no imbalance, no intraday
  adverse selection.
* **No borrow cost and no locate.** Long-short statistics are reported
  unfinanced. Hard-to-borrow refusals are not modelled at all.
* **No commissions, exchange fees, SEC Section 31 fee or FINRA TAF.**
* **No halts or limit-up/limit-down bands**, which are far more common in small
  caps and which in reality prevent exactly the fills a simulator will
  cheerfully print.
* **No adverse selection conditional on the signal.** The impact model is
  unconditional; if a signal correlates with the direction of contemporaneous
  institutional flow, real impact is worse than modelled.
* **No cross-name or portfolio-level impact.** Each order is priced alone.
* **Symmetric entry and exit**, an assumption daily bars cannot test.
* **Bid-ask bounce in the close prints.** Fills and returns both use the close,
  which is a print rather than a mid. Bounce is a larger fraction of a thin
  name's return than a liquid one's. It inflates the IC denominator
  asymmetrically across liquidity tiers — biasing measured IC toward zero more
  in the thin tier, which is the safe direction for a small-cap claim, but it is
  a real reason a thin-tier null may be a measurement failure rather than an
  absence. Nothing available here corrects it.

## References

* Corwin, S. A., & Schultz, P. (2012). A Simple Way to Estimate Bid-Ask Spreads
  from Daily High and Low Prices. *Journal of Finance*, 67(2).
* Almgren, R., Thum, C., Hauptmann, E., & Li, H. (2005). Direct Estimation of
  Equity Market Impact. *Risk*, 18(7).
* Amihud, Y. (2002). Illiquidity and Stock Returns: Cross-Section and
  Time-Series Effects. *Journal of Financial Markets*, 5(1).
* Abdi, F., & Ranaldo, A. (2017). A Simple Estimation of Bid-Ask Spreads from
  Daily Close, High, and Low Prices. *Review of Financial Studies*, 30(12).
  *Implemented and rejected; see above.*
* Fong, K. Y. L., Holden, C. W., & Trzcinka, C. A. (2017). What Are the Best
  Liquidity Proxies for Global Research? *Review of Finance*, 21(4).
  *Implemented and rejected; see above.*
