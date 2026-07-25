# SPEC.md — Stock-Grader merged specification

**Status:** normative build contract. Companions: `METRICS.md` (the 89-metric catalog),
`WEIGHTING.md` (the 33 weighting methods), `MANIFEST.json` (module build order).
`DATA-GROUND-TRUTH.md` overrides this file on data-source questions only.

Implementers should never need to guess. Where two source specs or two reviews disagreed, §1.3
records the decision and the reason.

---

## 1. Scope and decision log

### 1.1 What the system does

```
DataProvider -> SecuritySnapshot
  -> MetricEngine   -> dict[metric, raw float | None] + Coverage
  -> Normalizer     -> dict[metric, score 0..100]      (cross-sectional, or absolute if universe=1)
  -> Aggregator + WeightingMethod   (level 1: metrics -> PillarScore)
  -> Aggregator + WeightingMethod   (level 2: pillars -> composite 0..100)
  -> Aggregator + WeightingMethod   (level 3: profiles -> consensus grade)
  -> GradeScale     -> letter + percentile
  -> Uncertainty    -> confidence interval + coverage
  -> Explain        -> exact contribution decomposition, every grade point traced to a metric
```

### 1.2 Amendments to the shared architecture contract

The contract declared `types.py` fixed. Both reviews required fields that have nowhere to live.
The contract is hereby **amended**; these are the only additions, and nothing is removed.

| type | added field | why |
|---|---|---|
| `MetricResult` | `shape: Literal["monotonic","band"]` (derived, asserted `band ⟺ direction==0`) | the config validator's most important rejection — band metric routed to a monotonic normalizer — is otherwise unenforceable |
| `MetricResult` | `size_neutral_ok: bool = True` | blocks `size_neutral` on metrics that *are* size |
| `MetricResult` | `composite: bool = False`, `redundancy_group: str \| None` | excludes bundled scores from correlation/PCA/Choquet panels |
| `PillarScore` | `score_raw: float` | `[F11]` shrinks the score; both must be reported |
| `PillarScore` | `coverage_w: float`, `coverage_n: float`, `coverage_applicable: float` | `[F2]` needs weight coverage; count coverage is reported only |
| `PillarScore` | `contributions_raw: dict[str,float]` | pre-shrinkage Shapley values, for audit |
| `PillarScore` | `diagnostics: dict` | `n_eff`, weight turnover, imputed fraction |
| `GradeReport` | `letter_raw: str` | memoryless letter, alongside the hysteretic `letter` |
| `GradeReport` | `interval_sources: list[str]` | the CI does **not** cover data error, normalization estimation error, or universe definition; say so in the object |

Everything else the reviews asked for is routed through existing dicts and **must** live exactly
here: `GradeReport.meta` carries `universe_id`, `universe_size`, `pipeline_fingerprint`, `pit_mode`,
`seed`; `GradeReport.explain` carries `p_letter`, `letter_ci`, `weight_confidence`,
`weight_dispersion`, `n_effective_draws`, `elasticities`, `distance_to_next_letter`.

`GradeReport.graded` becomes `self.letter != NOT_RATED` with `NOT_RATED = "NR"` imported from
`constants.py`. The current implementation tests `letter != "N/A"`, which would make every
not-rated report register as graded — a straight sentinel mismatch.

`GradeReport.top_contributors` is rewritten per §6.6; the current `contribution * pillar_weight`
assumes a weighted arithmetic mean at level 2, which is **not** the default.

### 1.3 Decision log — conflicts between sources, and how they were settled

| # | Conflict | Decision |
|---|---|---|
| D1 | `c_top` computed over renormalized vs original pillar weights | **Original weights, dropped pillars contribute 0** (both reviews agreed). Plus the applicability refinement in §5.2, without which the fix drives every offline grade to `NR`. |
| D2 | Review A: replace the `c_min_pillar` hard-drop with shrinkage down to `c_w=0`. Review B: add a second presence gate at 0.60 | **Both, at different levels.** Continuous shrinkage replaces the pillar drop (drop only at `c_w == 0` exactly); the 0.60 presence gate operates on the *report*, forcing `NR`. They are complementary, not alternatives. |
| D3 | `zero_floor δ`: Review A says apply only for `rho <= 0`; Review B says apply uniformly in one place | **Uniformly, in one place** (`AggConfig` preprocessing). Review A's version makes `power_mean(rho=1) != weighted_arithmetic_mean` — the very test it set out to save — unless arithmetic floors too. Flooring everywhere makes both invariants true on a common vector, which is what a "one dial" claim requires. Additionally, normalizers are required never to emit exactly 0 or 100 (§4.3), so the floor is a numerical guard that does not bind in practice. |
| D4 | `quantile_bucket` antisymmetry: Review A fixes the bucket rule; Review B narrows the justification | **Fix the rule** (`b = clip(round(p·B − 0.5), 0, B−1)`) *and* adopt Review B's `B <= n` guard and its replacement test name. Making the property true beats documenting it away. |
| D5 | Weighting: fit on the target's available subset (Review B) vs one fit for the whole universe (Review A) | **Both, at different scopes** — universe-level filtering before `fit`, target-level restriction after. `WEIGHTING.md` §0.3. |
| D6 | Low-coverage column: weight 0 or `min_j(w_j)` | **`min_j(w_j)`** when the target has it; 0 only when the target genuinely lacks it. Both reviews independently preferred the nan_policy behaviour. |
| D7 | Negative weights: specify the signed path vs delete it | **Long-only everywhere**; signed coefficients become diagnostics. `WEIGHTING.md` §0.4. |
| D8 | Not-rated sentinel `NR` vs `N/A` | **`NR`**, single constant, existing property updated. |
| D9 | Hysteresis default | **Off** in the library; on only via `--hysteresis`; prior state caller-supplied. Closes source open-question 3. |
| D10 | Absolute cutoff table: ship the 90/85/80 ladder or derive it | **Derive** from the bundled reference universe at the percentile table's cut points. Closes source open-question 8 in the direction both the spec and Review B recommended, and makes hybrid an honest interpolation between "this universe" and "the reference universe". |
| D11 | Level-2 default `rho = 0` vs `0.5` | **`rho = 0`** (geometric) ships, because the cutoffs are now derived from the same pipeline (D10) so the distribution shift is absorbed by construction. `eval/` settles it empirically; source open-question 1 is now answerable with data rather than argument. |
| D12 | `WeightingMethod.confidence` location | **Required field on `WeightResult`**, never `series.attrs` (pandas drops attrs through most operations). Closes source open-question 2 and a hard blocker for `[F10]`. |
| D13 | Small-universe fallback: `piecewise_linear_absolute` (needs per-sector anchor tables) | **`historical_percentile` first**, `piecewise_linear_absolute` second. The calibration burden was unbudgeted and blocked the offline path entirely; the self-relative normalizer needs no calibration, no universe, and no survivorship-free constituent list. A build script generates the anchor tables. |

---

## 2. Cross-cutting invariants

1. **Determinism.** Identical inputs *and* identical prior state → identical grade. All seeding via
   `rng.py` (`blake2b`, never `hash()`). All iteration in `sorted(name)` order wherever an RNG is
   consumed. All sorts `kind="stable"` with the column name as documented secondary key. Verified by
   a two-subprocess test under `PYTHONHASHSEED=random`.
2. **No look-ahead.** Fundamentals lagged by `reporting_lag_days` (default 45 for quarterly, 90 for
   annual) from the **filing** date. `NormalizeContext` and `WeightingContext` both carry `asof` and
   the lag; `normalize()` raises `LookAheadError` if the universe panel's max date exceeds `asof`.
   Calibration tables, the Choquet interaction panel and the absolute cutoff table all carry an
   `asof` checked against the grading `asof` at load.
3. **Missing data is not badness.** A metric that could not be computed is never scored 0 and never
   scored 50-with-full-weight. It is dropped, its weight redistributed, and its absence is charged to
   coverage — which shrinks the score toward neutral and widens the interval. Grading a company `F`
   because its balance sheet failed to parse is the single most damaging failure mode of a scoring
   system, and it is what naive implementations do when `NaN` silently becomes 0.
4. **Inapplicable is not missing.** A bank has no current ratio because banks do not publish a
   classified balance sheet. `Coverage.NOT_APPLICABLE` leaves the coverage *denominator*; only
   `Coverage.MISSING` is penalised (§5.2).
5. **Every grade point traces to a metric.** `Σ over all metrics of the nested contribution ==
   composite − 50`, exactly, under every aggregator that declares `supports_shapley` (§6.5–6.6).
6. **Offline-first.** The system is fully functional with fundamentals alone; 30 of 89 metrics need
   prices and degrade to `NOT_APPLICABLE` when no price series exists.

---

## 3. Layer 0 — types, registries, constants

### 3.1 `constants.py`

```python
NEUTRAL_SCORE   = 50.0
SCORE_MIN, SCORE_MAX = 0.0, 100.0
SCORE_EPS       = 1e-6          # normalizers emit into [EPS, 100-EPS]; see §4.3
NOT_RATED       = "NR"
LETTERS = ["A+","A","A-","B+","B","B-","C+","C","C-","D+","D","D-","F"]
PILLARS = ["valuation","profitability","growth","health","quality",
           "efficiency","shareholder","risk","momentum","liquidity"]
MAD_CONSISTENCY = 1.4826        # 1 / Phi^-1(0.75)
IQR_CONSISTENCY = 1.34898       # 2 * Phi^-1(0.75)
ZERO_FLOOR      = 1.0           # aggregator score floor, delta
```

### 3.2 `rng.py`

```python
def stable_hash(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")

def make_rng(*, ticker, asof, profile, universe_id, pipeline_fingerprint, seed) -> Generator
def child_rng(parent_seed: int, *parts: str) -> Generator      # SeedSequence([seed, *stable_hash])
def pipeline_fingerprint(resolved_config: dict) -> str          # stable sha256 of the sorted config
```

`pipeline_fingerprint` is defined **once, here**, and reused for: the grade-scale mismatch warning,
hysteresis state invalidation, the RNG seed, and the weight cache key. The source specs had three
independent notions of it.

### 3.3 `errors.py`

`LookAheadError`, `NotPointInTimeError`, `CovarianceDegenerate`, `ConfigError`,
`StaleCalibrationWarning`, `RankReversalWarning`, `WeightingError`.

### 3.4 Registry extensions

The existing `stock_grader.registry` is kept and extended — the layer does **not** define second
decorators. `MetricSpec` gains `shape`, `size_neutral_ok`, `composite`, `redundancy_group`,
`normalizer_override`, `x_space`, `applicability`. Registration asserts
`shape == "band" ⟺ direction == 0`.

`Registry` gains `register_alias(old, new)`; resolving an alias emits `DeprecationWarning` and
returns the new object. All renames in §12.1 go through it.

`NormalizerSpec` gains `applies_direction: bool`, `stage: Literal["transform","group","terminal"]`,
`needs_groups: bool`, `needs_panel: bool`, `is_absolute: bool`.
`AggregatorSpec` gains `kind: Literal["pointwise","panel"]`, `supports_shapley: bool`,
`supports_signed_weights: bool`.

---

## 4. Layer 1 — normalization

### 4.1 `[F3]` Direction

```
s_out = s_inner        if direction == +1  or spec.applies_direction
s_out = 100 - s_inner  if direction == -1 and not spec.applies_direction
s_out = NaN            if s_inner is NaN
direction == 0  ->  the metric MUST route to a band normalizer (applies_direction=True)
```

Direction is the **score reflection** `s → 100 − s`. That reverses monotonicity regardless of whether
the underlying map is symmetric about 50 — which is all that is required, and is the honest
justification. The source spec's "every non-internal map is symmetric under `x → −x`" is false for
`quantile_bucket` at exact bucket boundaries (at `n=5, B=10` every `p_i·B` is an integer and the whole
vector is off by a full bucket) and for `winsorized_z` with `p_lo ≠ 1 − p_hi`. Both are fixed rather
than excused: §4.4 makes bucketing genuinely antisymmetric, and the validator rejects asymmetric
winsor bounds. The test is `test_score_of_negated_input_equals_100_minus_score`, not the tautological
`test_direction_antisymmetry`.

### 4.2 `[F4]` z → 0..100

```
map = "cdf" (default):  s = 100 * Phi(z)
map = "linear":         s = 50 + 50 * clip(z, -a, a) / a,   a = 2.5
degenerate: n_finite < 2, or scale < 1e-12*(1+|center|)  ->  s = 50.0 for all finite x
            + warning degenerate_dispersion:{metric}
```

This changes the existing implementation, which uses a linear clip at ±3σ. `config/normalize.yaml`
carries `z_map: cdf` and the change is recorded in the pipeline fingerprint.

### 4.3 Range invariant

**Every normalizer emits into `[SCORE_EPS, 100 − SCORE_EPS]` or `NaN`** — never exactly 0 or 100.
This is what makes the aggregator's `δ = 1.0` floor a non-binding numerical guard rather than a
modelling parameter (D3). `rank_minmax` and `gaussian_rank`, which both legitimately hit the
endpoints in their textbook form, are rescaled into the open interval. `piecewise_linear_absolute`
clips to it.

### 4.4 Normalizer catalog

Each entry: **stage / applies_direction / needs_panel / min_n**.

| name | stage | algorithm |
|---|---|---|
| `zscore` | terminal | `mu = mean(x_f)`, `sigma = std(x_f, ddof=1)`, `z = (x−mu)/sigma`, then `[F4]`. Outlier-sensitive by construction; never the default. |
| `robust_z` | terminal | `med = median(x_f)`; `sigma_r = 1.4826·MAD`. **If `MAD == 0`: `sigma_r = IQR/1.34898`. If `IQR == 0`: degenerate → all 50.** The current implementation falls back to `zscore` here, which reintroduces exactly the outlier sensitivity the fallback exists to prevent. `MAD == 0` occurs whenever >50% of the cross-section ties — `dividend_yield`, `zero_return_days`, `goodwill_to_assets` all do this routinely. |
| `winsorized_z` | terminal | **Winsorize by COUNT:** `m = max(1, floor(n·p_lo))`; clip at the `m`-th smallest and `m`-th largest order statistics. Then `mu, sigma` on the **clipped** vector (`ddof=1`), then `[F4]`. Quantile form is used only for `n >= 100`, where the two coincide. The source's quantile rule with auto-widen touched no observation for any `n < 20`, silently degenerating to plain `zscore` in the 20–30 band — where the universe is small *and* `winsorized_z` is half of the recommended default. Validator rejects `p_lo != 1 − p_hi`. |
| `percentile_rank` | terminal | Hazen `p_i = (r_i − 0.5)/n` on average ranks; `s = 100·p_i` rescaled into the open interval. `n = 1 → 50`. |
| `gaussian_rank` | terminal | Blom `p_i = (r_i − 0.375)/(n + 0.25)`; `z_i = Phi⁻¹(p_i)`; **`a_n = max over this cross-section of \|z_i\|`** (never a fixed clip); `s = 50 + 50·(1−2ε)·z_i/a_n`. The source's `clip(Phi⁻¹(1−1/2n), 2.0, 4.0)` had the floor binding for every small universe (at `n=5` the best name scored 79.5, not "near 100") and the ceiling binding above `n≈15800`, emitting exactly 0. |
| `rank_minmax` | terminal | `s = ε + (100−2ε)·(r_i − 1)/(n − 1)`; `n = 1 → 50`. |
| `quantile_bucket` | terminal | `p_i` Hazen; **`b_i = clip(round(p_i·B − 0.5), 0, B−1)`**, which satisfies `b(p) + b(1−p) = B−1` for all `p` including integer `p·B`; `s = 100·(b_i + 0.5)/B`. Guard `B <= n` else warn `buckets_exceed_universe` and reduce `B`. |
| `sigmoid` | terminal | `s = 100·expit(z/τ)`, `z` from `inner_z` (default `robust_z`), `τ = 1.0`. Use `scipy.special.expit`. |
| `tanh_squash` | terminal | `s = 50·(1 + tanh(z/τ_t))`, `τ_t = 2.0`. **Identical family to `sigmoid`:** `tanh(u) = 2·expit(2u) − 1`, so `tanh(τ_t)` ≡ `sigmoid(τ_l = τ_t/2)`. Both kept for familiarity; the validator does not treat them as independent choices. |
| `double_sigmoid` | terminal, **applies_direction** | §4.5 |
| `piecewise_linear_absolute` | terminal, **applies_direction**, needs no panel | §4.6 |
| `historical_percentile` | terminal, needs no panel | §4.7 |
| `historical_z` | terminal, needs no panel | `z = (x_now − median(hist)) / (1.4826·MAD(hist))`, then `[F4]`. `min_obs = 20`. |
| `yeo_johnson_z` | terminal | `sklearn.preprocessing.PowerTransformer(method="yeo-johnson", standardize=True)` fitted on the cross-section, then `[F4]`. The only variance-stabilizing transform in the set; everything else clips or ranks. Handles metrics straddling zero. |
| `winsor_rank` | terminal | `s = α·winsorized_z + (1−α)·percentile_rank`, `α = 0.5`. **The default for any universe with `n >= 30`.** NaN in either operand → NaN. |
| `sector_neutral` | **group** | §4.8 |
| `industry_neutral` | **group** | §4.8, nested |
| `size_neutral` | **transform** | §4.9 |
| `constant_50` | internal | `s = 50.0` where `x` is finite, NaN elsewhere. Not user-selectable; emitted on degeneracy so downstream code never special-cases `None`. |
| `minmax` | terminal | `s = 100(x − min)/(max − min)`; `max == min → 50`. Provided for completeness; the validator warns whenever selected without `p_lo`/`p_hi`, because two outliers otherwise determine the entire mapping. |

### 4.5 `double_sigmoid` — the band scorer

```
u = (x - lo)/w_lo ;  v = (hi - x)/w_hi ;  g(x) = expit(u) * expit(v)
normalize_peak = True   (DEFAULT — stated explicitly)
s = f + (P - f) * g(x)/g(x*)
x* : if w_lo == w_hi then (lo+hi)/2 in closed form; else the root of
     dL/dx = (1 - expit(u))/w_lo - (1 - expit(v))/w_hi
BRACKETING: evaluate dL/dx on a 257-point grid over [lo, hi]; call brentq only on a sign-changing
sub-bracket; if none exists take the grid argmax of g and warn band_peak_grid_fallback
VALIDATE AT CONFIG LOAD: lo < hi, w_lo > 0, w_hi > 0.  Defaults w_lo = w_hi = (hi-lo)/4.
```

`brentq` requires a sign change on `[lo, hi]`; with strongly asymmetric widths `dL/dx` can be positive
at both endpoints and it raises `ValueError` — an uncaught crash on a config value the spec explicitly
lets the user set.

**Corrected documented behaviour:** with `normalize_peak=True` and the default widths, the peak scores
100, the band edges score ≈63, and values far outside the band asymptote to `f`. The source spec
claimed edges ≈50 *and* peak = 100, which cannot both hold (`g(edge)/g(peak) = 0.491/0.776 = 0.633`).
Edges-at-50 is a different parameterization (`w ≈ (hi−lo)/8`) and must be specified numerically, not
asserted.

**Absolute/cross-sectional reconciliation.** `double_sigmoid` and `piecewise_linear_absolute` are
absolute maps; `winsor_rank` is cross-sectional. Averaging a band score capped by its own shape
against rank scores spanning the full range biases every pillar containing a band metric. **Rule:**
when a universe with `n >= min_universe` is available, absolute scores are **percentile-matched** —
mapped through the universe's ECDF of that metric's absolute scores — before entering the pillar.
Warning `absolute_score_ecdf_matched:{metric}`. Without a universe, the raw absolute score is used and
`absolute_score_unmatched` is recorded.

### 4.6 `piecewise_linear_absolute`

```
anchors A = [(v_1,s_1) ... (v_k,s_k)], v strictly increasing, s in [EPS, 100-EPS], s may be
increasing, decreasing or non-monotone (decreasing encodes direction = -1)
for v_j <= x <= v_{j+1}:
    s = s_j + (s_{j+1} - s_j) * (t(x) - t(v_j)) / (t(v_{j+1}) - t(v_j))
    t = identity (x_space=linear) or ln (x_space=log; requires all v > 0 and x > 0)
x < v_1 -> s_1 ; x > v_k -> s_k        (extrapolate=clamp, default)
                                        or continue the end slope (extrapolate=linear), then clip
```

**Calibration recipe (corrected).** `DEFAULT_PCTS = [1,5,10,25,50,75,90,95,99]` and
**`DEFAULT_SCORES` are built by evaluating `winsor_rank` on the frozen reference universe and reading
off the scores at those percentiles** — so the absolute path reproduces the cross-sectional path by
construction. The source spec paired the percentiles with `[2,8,15,30,50,70,85,92,98]`, an
undocumented S-compression under which the same stock graded alone versus inside the universe differs
by up to 5 points per metric, and the small-universe fallback introduces a discontinuity in the grade
at exactly `n = 30`. Unit test: `|piecewise(x) − winsor_rank(x within reference universe)| < 2` at the
anchor percentiles.

Stored per sector in `config/calibration/<sector>.yaml` with `asof`, `universe_id` and
`max_age_days = 400`; stale → warning. Generated by `scripts/build_calibration.py`, which ships the
generated files. **Survivorship:** the calibration universe must be the point-in-time constituent list
*including delisted names*, or every absolute cutoff is biased optimistic.

Keying is by **sector alone** in v1. `(sector, size_bucket)` is the correct refinement — ratio
distributions for mega- and micro-caps differ materially — but it doubles the recalibration burden;
`eval/` decides, and the file format already carries a `size_bucket: all` field so the upgrade is
additive.

### 4.7 `historical_percentile` — the calibration-free absolute path

```
p = (rank of x_now within the security's own trailing window, Hazen) ; s = 100*p
window = 10 years of the security's own metric history (from panel_history or the fundamentals frame)
min_obs = 20 ; fewer -> None, walk to the next fallback
```

Answers "is this P/E cheap versus its own 10-year range" with no universe, no calibration file and no
survivorship-free constituent list. **This is the first small-universe fallback**, ahead of
`piecewise_linear_absolute` (D13).

### 4.8 `[F6]` Group neutralization with James–Stein shrinkage

Guards are **ordered**; the source spec divided before testing the denominator.

```
1. groups_ok = { g : n_g >= 2 }
   if |groups_ok| < 2:   lambda_g = 0 for all g ; warn insufficient_groups_for_shrinkage ; return
                         the global standardization
2. sigma2 = sum_{g in groups_ok} (n_g - 1) s_g^2 / sum_{g in groups_ok} (n_g - 1)
   if that denominator is 0:  lambda = 0 ; warn no_within_group_variance ; return
3. tau2 = max(0, Var_{g in groups_ok}(m_g, ddof=1) - mean_{g in groups_ok}(s_g^2 / n_g))
   if tau2 <= 1e-12:  lambda_g = 0 for all g ; return      # SHORT-CIRCUIT before any division
                                                            # this is the CORRECT answer, not an error
4. k_hat = clip(sigma2 / tau2, 0.0, 1000.0)                 # lower bound 0, NOT 1
5. lambda_g = n_g / (n_g + k_hat) ; if n_g < n_min (5): lambda_g := 0 (full pooling) + warn
6. m_hat_g = lambda_g m_g + (1 - lambda_g) m_0
7. SCALE: shrunk on the LOG-VARIANCE scale with its OWN weight
   y_g = ln(s_g^2) ; sigma2_var = 2/(n_g - 1) ; tau2_var = max(0, Var_g(y_g) - mean_g(2/(n_g-1)))
   lambda_var_g = tau2_var / (tau2_var + sigma2_var)        (0 if tau2_var <= 0)
   ln s_hat_g^2 = lambda_var_g * y_g + (1 - lambda_var_g) * ln s_0^2
   if s_hat_g < 1e-12*(1+|m_0|): s_hat_g = s_0
8. z_i = (x_i - m_hat_{g(i)}) / s_hat_{g(i)} , then the inner's [F4] map
```

Four corrections. (a) The lower clip `k >= 1` forced `lambda = 0.83` at `n_g = 5` even when
between-group spread genuinely dominates — pooling away a real sector effect exactly when
neutralization is most warranted. (b) `k_hat` was computed *before* the `tau2 <= 0` branch, i.e. a
division by zero. (c) `Var_g` had no `ddof` and no `G >= 2` gate: with one sector in the universe
(routine for a sector screen or a small fixture) the unbiased variance of one value is `NaN`,
`max(0, NaN − x) = NaN`, and **every score in the universe becomes NaN** — the pillar silently
vanishes through `[F1]` rather than raising. (d) The scale was shrunk with the *mean's* `lambda`; the
sampling variance of a group mean is `sigma²/n` while that of a group variance is `≈2σ⁴/(n−1)`, so
using one for both systematically under-shrinks the scale in small groups, and an under-shrunk small
`s_hat_g` pushes a 6-name industry to the extremes of the 0..100 range purely from estimation noise —
the exact noise amplification `industry_neutral` exists to prevent. (A simpler, strictly safer
alternative — force `s_hat_g = s_0` always, shrinking only the location — is exposed as
`scale_shrinkage: none` and is what most practitioner sector-neutralization does.)

**Rank inners** blend plotting positions instead: `p_hat_i = λ_g·p_within_g + (1−λ_g)·p_global`.

**Composite inners (`winsor_rank`)** — the default configuration, for which the source spec defined no
behaviour at all: apply the group rule **independently to each component with its own regime**
(location-scale path for the `winsorized_z` leg, plotting-position path for the `percentile_rank`
leg), then blend the two resulting **scores** with `α`, exactly as unwrapped `winsor_rank` does.
Tested by `test_sector_neutral_winsor_rank`.

`industry_neutral` is nested: `m_hat_sec = λ_sec·m_sec + (1−λ_sec)·m_0`, then
`m_hat_ind = λ_ind·m_ind + (1−λ_ind)·m_hat_sec`, with `k_hat` estimated per level. Missing industry
falls back to sector, missing sector to global, each recorded.

`k_hat` is estimated **per metric**, not per pillar. Noisier in small universes but correct;
`eval/` may revisit (source open-question 4).

### 4.9 `size_neutral`

```
l_i = (log10(mcap_i) - mean) / std
X = [1, l] (degree 1) or [1, l, l^2] (degree 2), PLUS SECTOR DUMMIES
fit statsmodels.RLM(y, X, M=HuberT(t=1.345)).fit() ; e_i = y_i - X_i beta_hat ; feed e to inner
n_finite < min_n (20) or Var(l) == 0  ->  identity passthrough + warning
metric.size_neutral_ok == False       ->  REJECTED by the config validator
```

Sector dummies are required: market-cap distributions differ enormously by sector, so a *global* size
regression leaves sector–size confounding that a subsequent sector demean cannot remove. Huber, not
OLS — OLS is wrecked by the very outliers being controlled for.

### 4.10 Wrapper stacking — the composition contract

`NormalizerSpec.stage` defines a strict three-part pipeline:

```
[ transform* ]  ->  [ exactly one group ]  ->  [ terminal ]
size_neutral        sector_neutral OR         winsor_rank, robust_z, ...
                    industry_neutral
```

* **transform** stages return values **in the metric's own units** and may chain.
* Exactly **one** group stage may be applied, and it is **the only thing that calls the inner's
  value→score map**.
* The terminal stage produces the 0..100 score.

The validator rejects `sector_neutral` and `industry_neutral` in the same stack with
`redundant_neutralizer_stack` — `industry_neutral` is *defined* as a nested industry→sector→global
chain, so stacking `sector_neutral` beneath it removes the sector effect **twice** and the residual is
over-differenced and uninterpretable. It also rejects applying `[F4]` twice, which is not idempotent.

Default stack: `size_neutral(degree=1, sector_dummies=True) → sector_neutral(inner=winsor_rank)`.

### 4.11 Leave-one-out option

`NormalizeConfig.loo: bool = False`. When true, the center/scale/ranks for security `i` are computed
**excluding `i`**. At `m` near the 30-name minimum the graded security contributes >3% of the mean,
MAD and rank distribution it is scored against, so the pipeline is not strictly monotone in its own
metric value and the score is mildly self-referential. `loo=True` makes the whole pipeline exactly
monotone in each security's own inputs.

### 4.12 Vintage dispersion

`NormalizeContext` carries per-security `staleness_days` (asof minus the effective fundamental date).
Cross-sectional normalization implicitly assumes a common information vintage, but the 45/90-day
reporting lag means a company that filed yesterday is compared against one whose data is 89 days old.
When `std(staleness_days) > staleness_dispersion_warn` (default 45), record
`universe_vintage_dispersion`.

### 4.13 Monotonicity invariant (stated so the test is not written to pass by accident)

Strict monotonicity is **false** for every rank normalizer (`rank_minmax` pins the top at the maximum
however much larger it gets), every clipping normalizer, and `quantile_bucket` (flat within a bucket).
The true, testable invariant is **weak monotonicity under leave-one-out perturbation**: hold the other
`n−1` values fixed, vary `x_i`, assert the score is non-decreasing.

---

## 5. Coverage — the arithmetic that drives everything downstream

### 5.1 `[F1]` Weight renormalization, and the two zero-weight predicates

```
(a) CONFIGURED vector over the full metric set M sums to <= 0  (a config error):
        fall back to equal weights over M ; warn zero_weight_vector ; CONTINUE   <- checked FIRST
(b) after masking to the observed set O, W_O = sum_{j in O} w_j <= 0
    (all weight sits on missing metrics):
        pillar score = NaN ; warn no_observed_weight ; pillar dropped
otherwise wt_i = w_i / W_O for i in O ; wt_i = 0 otherwise
```

The source spec and its own edge-case list gave two different behaviours for the same predicate; they
are two *different* predicates and (a) is checked first.

### 5.2 `[F2]` Coverage — corrected, with applicability

```
Per pillar p, over its metric set M_p:
    A_p = { j : coverage_j != NOT_APPLICABLE }            # the APPLICABLE set
    O_p = { j in A_p : score_j is not NaN }               # the OBSERVED set
    W_A = sum_{j in A_p} w_j ;  W_O = sum_{j in O_p} w_j
    c_w(p) = W_O / W_A          (1.0 if W_A == 0 and A_p is empty -> pillar itself NOT_APPLICABLE)
    c_n(p) = |O_p| / |A_p|                                 # reported only

Top level, over ALL pillars P (not just the surviving ones):
    P_app = { p : pillar p is applicable at all }
    c_top = sum_{p in P_app} w_p_orig * c_w(p) / sum_{p in P_app} w_p_orig
            with c_w(p) := 0 for any pillar that is NaN
    c_presence = sum_{p in P_app, observed} w_p_orig / sum_{p in P_app} w_p_orig
```

**This is the single most important correction in the merge.** The source formula summed over
*observed* pillars using *renormalized* weights that already sum to 1, so a dropped pillar vanished
from the arithmetic entirely: a report with 4 of 5 pillars completely absent and the survivor at full
coverage computed `c_top = 1.0`, passed the `NR` gate, got widening factor `f = 1.0` and no shrinkage.
Combined with the geometric level-2 default this was actively perverse — a company scoring 5 on
solvency gets composite 43.7 on the spec's own `[90,90,90,5]` example, while a company whose solvency
pillar is *missing* gets 90. **Missing data became strictly better than bad data**, which nullifies
the entire stated rationale for the geometric default and inverts the purpose of the not-rated gate.

The `P_app` refinement is what makes the fix survivable offline. Per `DATA-GROUND-TRUTH.md` no free
price source is reachable, so `risk`, `momentum` and `liquidity` — 30 of 89 metrics and, in
`all_weather`, 8% of the pillar weight — are structurally inapplicable. Counting them as *missing*
would drive every offline grade to `NR`. They leave the denominator instead, and
`GradeReport.meta["pillars_not_applicable"]` records it so the user sees what was not measured.

**Gates:**

```
c_w(p) == 0 exactly       -> pillar dropped (the ONLY hard drop)
0 < c_w(p) < 1            -> pillar SHRUNK per [F11], never deleted
c_presence < 0.60         -> letter = NR
c_top      < 0.35         -> letter = NR
n_observed_pillars < 3    -> warn composite_from_few_pillars
```

Replacing the `c_min_pillar = 0.40` hard drop with continuous shrinkage is D2: under the hard drop, a
company whose solvency data is 39% covered had its solvency pillar **deleted** and was graded on the
rest, so missing solvency scored strictly better than bad solvency. A 0.2-covered pillar now becomes
`50 + 0.45(s − 50)` — a weak signal, not a deleted one.

Required tests: `test_dropped_pillar_lowers_c_top`, `test_missing_pillar_is_not_better_than_bad_pillar`,
`test_four_of_five_pillars_absent_gives_NR`, `test_price_free_grade_is_not_NR`.

### 5.3 `[F11]` Coverage shrinkage — pillar level ONLY

```
s_adj = 50 + (s_raw - 50) * c_w ** gamma        gamma = 0.5 (config)
```

Reported as `PillarScore.score` (adjusted) alongside `score_raw`. Applied at **exactly one level**;
applying it at the top too would square the penalty, and a unit test enforces that.

**Honest justification.** This is a *calibrated heuristic penalty with a tunable exponent*. It is
**not** the Bayes posterior mean under a diffuse prior — that shrinkage factor is
`tau²/(tau² + sigma²/n_eff) = c/(c + k)`, which is concave and asymptotes to 1, and is not `c^gamma`
for any gamma. The families disagree materially where it matters (at `c = 0.5`: `0.707` versus `0.33`
at `k=1` or `0.83` at `k=0.1`). Shipping the formula with the Bayesian sentence attached would lead a
future maintainer to "derive" a different gamma and get a different answer. The Bayesian form
`s_adj = 50 + (s_raw − 50)·c/(c + k_cov)` with `k_cov = 0.214` (matching 0.7 at `c = 0.5`) is exposed
as `shrinkage_form: bayes`.

---

## 6. Layer 2 — aggregation

### 6.1 Registry split

```python
PointwiseAggregator: (s: pd.Series, w: pd.Series, cfg) -> AggResult    # per security
PanelAggregator:     (X: pd.DataFrame, w: pd.Series, cfg) -> pd.Series # whole universe
AggregatorSpec.kind in {"pointwise", "panel"}
aggregate(name, s, w, cfg)          # dispatches pointwise only
aggregate_panel(name, X, w, cfg)    # dispatches panel only
```

`topsis` takes an `m × n` panel and returns a score **per security**, so it cannot be invoked through
the pointwise signature and `AggResult` cannot describe it. The config validator rejects panel
aggregators when `m == 1` rather than relying on a runtime fallback.

`AggResult` = `(score, score_raw, weights_effective, coverage_w, coverage_n, universe_id,
universe_size, warnings)`.

### 6.2 Weight preprocessing — one place

`AggConfig` preprocessing, applied before **every** aggregator in the mean family:

```
w  := project non-negative, renormalize per [F1]
s' := maximum(s, delta),  delta = ZERO_FLOOR = 1.0        # UNIFORM: arithmetic floors too
```

Uniform flooring is D3. Without it, `s = [0.5, 0.5]` gives `A_unfloored = 0.5` but `G_floored = 1.0`,
so `G > A` and `test_am_gm_hm_ordering` fails; and `power_mean_ces(rho=1) != weighted_arithmetic_mean`
whenever any score is below 1.0. The AM–GM–HM invariant is stated on the **common floored vector**:
`H(s') <= G(s') <= A(s')`. Because §4.3 forbids normalizers from emitting exactly 0, the floor does
not bind in practice; when it does, `zero_floor_binding:{metric}` is recorded.

### 6.3 `[F7]` The master aggregator — weighted power mean

```
rho != 0:      M_rho = exp( (1/rho) * logsumexp_i( ln(wt_i) + rho * ln(s'_i) ) )
|rho| < 1e-6:  M_0   = exp( sum_i wt_i * ln(s'_i) )
```

`rho = 1` arithmetic, `0` geometric, `−1` harmonic, `→ −inf` min, `→ +inf` max. Elasticity of
substitution `1/(1−rho)`. `M_rho` is non-decreasing in `rho`, so harmonic ≤ geometric ≤ arithmetic.
Validator restricts `rho ∈ [−5, 2]`.

**Why log-space, honestly.** *Not* because the direct form overflows: scores are floored into
`[1, 100]`, so `s^rho` for `|rho| <= 5` spans `[1e-10, 1e10]` while float64 overflows near `1.8e308` —
you would need `|rho| > 150`. The source spec asserted the overflow claim twice; a maintainer who
checks it will find it false and may conclude the log-space path is unnecessary, or "fix" the
validator range on a bogus constraint. The real reasons: log-space avoids catastrophic cancellation
for strongly negative `rho`, where terms span 10+ orders of magnitude, and it is the numerically
stable path for the `rho → 0` limit. The `[−5, 2]` range is a **modelling** bound (elasticity in
`[0.167, ∞)`), not a floating-point one.

### 6.4 Pointwise aggregator catalog

| name | algorithm |
|---|---|
| `weighted_arithmetic_mean` (alias `weighted_mean`) | `A = Σ wt_i s'_i`. **Default at metric → pillar** — metrics inside a pillar are near-substitutes, and `[F8]` collapses to the exact closed form `φ_i = wt_i(s'_i − 50)`, giving perfect explainability at zero extra compute. |
| `weighted_geometric_mean` | `G = exp(Σ wt_i ln s'_i)`. Power mean at `rho = 0`. |
| `harmonic_mean` | `H = 1 / Σ(wt_i / s'_i)`. Power mean at `rho = −1`; maximally punitive on the weakest input. Appropriate only for true gating pillars. |
| `power_mean_ces` (alias `ces`) | §6.3. **Default at pillar → final with `rho = 0`.** Pillars are *not* substitutes: `[90,90,90,5]` equally weighted gives arithmetic 68.75 but geometric 43.7, and a company with a 5/100 solvency pillar should not grade B. This deliberately deviates from the MSCI/Sustainalytics-style arithmetic convention; documented in `docs/aggregation.md`. |
| `weighted_median` | Sort ascending with weights; `W_k = Σ_{j<=k} wt_j`; `k* = min{k : W_k >= 0.5}`. If `|W_{k*} − 0.5| < 1e-12` return `(s_(k*) + s_(k*+1))/2`, else `s_(k*)`. The exact-tie midpoint rule is required for determinism (equal weights on an even count is the common case). |
| `trimmed_mean` | Sort ascending; `F_i` = cumulative weight, `F_0 = 0`; retained mass `u_i = max(0, min(F_i, 1−t) − max(F_{i−1}, t))`; `T = Σ u_i s'_i / Σ u_i`. `t >= 0.5` → weighted median. **The boundary observation is FRACTIONALLY clipped**; the naive "drop the top-k and bottom-k items" is incorrect under unequal weights — trimming a 30%-weight metric would remove 30% of the mass when 10% was requested. |
| `soft_min` | `SM_T = −T·logsumexp(ln wt_i − s'_i/T)`, `T = 10` score points. Provable bounds `min_i s'_i <= SM_T <= Σ wt_i s'_i`, so no clamping is needed — the claim that `soft_min` can fall below the true min is wrong when the weights sum to 1. `T → 0` gives the hard min (and becomes weight-independent); `T → ∞` the arithmetic mean. |
| `owa` | Sort descending; RIM quantifier `Q(r) = r^q`, `v_k = Q(k/n) − Q((k−1)/n)`, `q = π/(1−π)`, `π` = pessimism (orness `= 1−π`). **Branch to hard max/min at `π = 0`/`1`** rather than letting `q` blow up. OWA weights **positions, not metrics**, so it ignores per-metric weights entirely — the validator warns if non-uniform weights are configured alongside plain `owa`. |
| `wowa` | Torra's weighted OWA: sort descending carrying `wt_(k)`; `v'_k = Q(Σ_{j<=k} wt_(j)) − Q(Σ_{j<k} wt_(j))`. Reduces to `weighted_arithmetic_mean` at `π = 0.5` and to `owa` under uniform weights — both required unit tests. The aggregator to reach for when you want both "ROE matters more" and "weaknesses hurt". |
| `choquet_2additive` | §6.7 |
| `lexicographic_gate` | *(new)* `composite := min(composite, cap(s_gate))` with a configured `gate_pillar` and a piecewise cap curve. `soft_min` and `rho → −5` approximate a veto but never actually gate; this is the only formulation that can guarantee "no company with solvency < 20 grades above C". |

### 6.5 Panel aggregator: `topsis`

```
X (m x n) of already-directed 0..100 scores
r_ij = x_ij / 100                      <- NOT the L2 column norm
v_ij = wt_j * r_ij
A+_j = max_i v_ij ; A-_j = min_i v_ij
D+_i = ||v_i - A+||_2 ; D-_i = ||v_i - A-||_2
C_i  = D-_i / (D+_i + D-_i) ; score = 100 * C_i
```

**Vector normalization removed.** The classical `x_ij / sqrt(Σ_k x_kj²)` is applied to columns that
are *already commensurate* 0..100 scores, and dividing by the column L2 norm silently rescales
column `j`'s configured weight by `1/||x_j||` — where `||x_j||` depends on the column **mean**, not its
dispersion. A metric on which the universe scores near 100 gets a norm ~2× that of one scoring near
50, so its configured weight is silently halved, for no reason. The user's weights would not be the
weights applied. If the classical form is ever restored for literature fidelity, the **effective**
weights `w_j/||x_j||` must be emitted into `explain`.

`supports_shapley = False`; excluded from the efficiency invariant (`v(all-50)` is not 50 for TOPSIS —
it is `0/0` on a degenerate panel, so `test_shapley_efficiency_all_aggregators` could never pass with
it included).
`m == 1` → `D+ = D− = 0` → undefined → fall back to `weighted_arithmetic_mean` + warning.
**Rank reversal:** adding or removing one security changes everyone else's score, so the report MUST
carry `universe_id`, and TOPSIS scores must never be compared across universes or across dates with
different constituents.

### 6.6 `[F8]` Contributions — exact, nested, shrinkage-aware

**Characteristic function** (weights held FIXED, no renormalization):

```
v(S) = f(s_tilde ; wt),  s_tilde_i = s_i for i in S, else 50.0
v(empty) = f(all-50 ; wt) = 50 for every pointwise aggregator here
phi_i = sum_{S subset N\{i}} [ |S|!(n-|S|-1)!/n! ] ( v(S u {i}) - v(S) )
efficiency:  50 + sum_i phi_i == score
exact enumeration for n <= 12 ; otherwise 2000 seeded permutations (antithetic pairing, §WEIGHTING 6.5)
```

The "replace with 50, keep weights" counterfactual is chosen over "drop and renormalize" because only
the former makes `v` additive for the arithmetic mean, yielding `φ_i = wt_i(s_i − 50)` exactly.

**Two required corrections, both absent from the source:**

**(a) Shrinkage rescaling.** `[F11]` is affine about the same neutral point as `[F8]`, so:

```
phi_i_adj = phi_i * c_w ** gamma
```

Without it, `Σ_i φ_i = score_raw − 50` while the pillar reports `score_adj`, so `verify_efficiency`
fails on any pillar with `c_w < 1`, the "every grade point traced" guarantee is void, and
`top_contributors` overstates every driver by `1/c_w^gamma`.

**(b) Level-2 → level-1 chain rule.** With the geometric level-2 default, `φ_p ≠ wt_p(s_p − 50)`, so
metric contributions scaled by `wt_p` do not sum to `composite − 50`, and the ranking of "what moved
the grade" is wrong in a *predictable* direction — it understates the influence of the weakest
pillar, precisely the effect the geometric mean was chosen to create.

```
compute phi_p over pillars via [F8] on the level-2 aggregator
scale_p = phi_p / (s_p_adj - 50)          if |s_p_adj - 50| > 1e-9 else 0.0
metric_contribution(i in p) = phi_i_adj * scale_p
IDENTITY (hard test): sum over all metrics == composite - 50
```

`GradeReport.top_contributors` returns these nested contributions, not `contribution × pillar_weight`.

`verify_efficiency` asserts against the **adjusted** score, and the coverage-shrunk case is included
in `test_shapley_efficiency_all_aggregators` (which iterates only `supports_shapley` aggregators).

### 6.7 `choquet_2additive`

```
C(s) = sum_{i<j, I_ij>0} min(s_i,s_j) I_ij
     + sum_{i<j, I_ij<0} max(s_i,s_j) |I_ij|
     + sum_i s_i ( phi_i - 0.5 sum_{j != i} |I_ij| )
monotonicity: phi_i - 0.5 sum_{j != i} |I_ij| >= 0 for all i   (necessary and sufficient)
Mobius: phi_i = a_i + 0.5 sum_j a_ij ; I_ij = a_ij ; coefficients provably sum to 1
```

`I = 0` reduces **exactly** to `weighted_arithmetic_mean` — a required unit test. `I_ij < 0` =
redundancy (ROE vs ROIC): the pair contributes via `max`, killing double-counting. `I_ij > 0` =
complementarity: contributes via `min`.

**Prior, corrected on both counts:**

```
1. rho = Spearman over the calibration panel of the DIRECTION-ADJUSTED NORMALIZED SCORES (post-[F3]),
   never of raw values.  Metrics with composite == True are EXCLUDED from the panel.
   Because it is Spearman it is invariant to the choice of monotone normalizer -- a feature.
2. I_ij = -gamma * rho_ij,  gamma = 0.5
3. PER-PAIR CAP first:  |I_ij| <= 0.5 * min(phi_i, phi_j) / max(1, n-1) ; warn choquet_pair_capped:{i,j}
4. restrict to the SUPPORT {i : phi_i > 1e-9}, renormalize phi over it, zero the excluded
   rows/columns ; warn choquet_support_reduced
5. THEN the global monotonicity bisection on a scale alpha in (0,1], 50 steps to 1e-9
6. pre-check: any pair with |rho| > 0.95 -> flag metric_pair_near_duplicate (a candidate for removal
   upstream, not something a fuzzy measure should be papering over)
```

Three fixes. (a) Computing `rho` on **raw** values flips the sign for any `direction = −1` metric, so a
genuinely redundant pair (debt/equity and interest coverage) gets `I_ij > 0`, is treated as
**complementary** and aggregated via `min` — the exact opposite of the intent, making the grade *more*
punitive on redundancy. (b) A single global `alpha` means one perfectly-redundant pair (P/E and
earnings yield; ROE and ROIC; any ratio also bundled inside an Altman/Piotroski composite in the same
pillar) drives `alpha → 0` and **silently collapses the entire interaction structure to zero**,
reverting Choquet to the arithmetic mean with no warning. (c) Zero-weight metrics are routine (a
sparse weighting method, or a metric renormalized to 0 by `[F1]`), and for any `i` with `phi_i = 0`
the monotonicity constraint forces `alpha = 0` — same silent collapse.

**Offline availability** (source open-question 6, closed): a frozen, versioned metric–metric Spearman
matrix ships as `config/interactions/default.yaml`, generated by `scripts/build_interactions.py` from
the fixture panel and carrying an `asof` checked to be strictly prior to the grading `asof` at load —
the same look-ahead discipline `load_calibration` already applies. Without it Choquet is dead code in
every default single-name run.

---

## 7. Layer 3 — weighting

Fully specified in `WEIGHTING.md`. Interface summary for this document:

* `compute_weights(name, X, ctx) -> WeightResult`, which carries `weights`, `confidence`,
  `fallback_used`, `dispersion`, `diagnostics`.
* Fitted **once per (level, group, asof)** over the universe; restricted per target.
* Long-only at both levels; the weighting layer does **not** compute scores or contributions.
* Three levels: `metric_to_pillar`, `pillar_to_final`, `profile_to_final`.

---

## 8. Layer 4 — grade scale

### 8.1 The 13-letter ladder

Percentile cut points `pi_L` (the primary table):

```
A+ 96  A 90  A- 84  B+ 76  B 68  B- 60  C+ 50  C 40  C- 30  D+ 20  D 12  D- 6  F otherwise
```

Implied population shares `4/6/6/8/8/8/10/10/10/10/8/6/6 = 100%`, bell-shaped around C+/B−.

**Absolute cutoffs are DERIVED, not hand-set** (D10):

```
theta_abs(L) := Q_reference(pi_L / 100)     over the bundled reference universe's composite scores,
                computed with the SAME pipeline (same normalizer, aggregator, rho, weights)
```

`config/grade_scale.yaml` stores the derived table together with the `pipeline_fingerprint` that
produced it; the loader **warns on mismatch** and `scripts/derive_cutoffs.py` regenerates it. Shipping
the hand-set 90/85/80 ladder alongside a geometric level-2 default and rank normalizers would put
almost nothing above 90 — the source spec identified this in its own notes and still shipped the
ladder.

### 8.2 The three modes and `[F9]`

```
absolute:        letter = highest L with score >= theta_abs(L)
cross_sectional: p = 100*(rank_avg(score) - 0.5)/m          # Hazen
                 letter = highest L with p >= pi_L
                 requires m >= min_universe (30) else falls back to absolute
                 + warning universe_too_small_for_percentile
hybrid:          theta_hyb(L) = beta*theta_abs(L) + (1-beta)*Q_universe(pi_L / 100.0)
                 Q_universe = np.quantile(finite universe scores, q, method="hazen")
                 letter = highest L with score >= theta_hyb(L) ;  beta default 0.5
```

Two corrections. (a) **`pi_L / 100.0`** — the table is in percent (`A+ = 96`) while `np.quantile` takes
a fraction; passing 96 raises `ValueError`. (b) **`method="hazen"`**, which is the exact inverse of
`p = (r − 0.5)/m`. The source used numpy's default type-7, whose implied plotting position is
`(r−1)/(m−1)` — a different inverse — so `score >= Q_type7(pi_L)` and `Hazen_percentile(score) >= pi_L`
do not select the same set, and hybrid at `beta = 0` does **not** reduce to pure cross-sectional.
`test_hybrid_reduces_to_pure_modes` asserts the **letter set over the whole universe** is identical at
`beta = 0` and at `beta = 1`, not merely that one ticker matches.

Compute over `np.asarray` of **finite** universe scores only — `np.quantile` returns NaN if any element
is NaN.

**Ordering assertion: non-decreasing, with a tie nudge.** A universe with many tied composites (small
`m`, or `constant_50` firing widely) legitimately produces `Q(0.96) == Q(0.90)`, and asserting *strict*
increase would raise on a perfectly ordinary input. Walk from the bottom applying
`theta_hyb(L) := max(theta_hyb(L), theta_hyb(L_below) + 1e-9)` and warn `hybrid_thresholds_tied`.

Comparisons are `>=` everywhere (inclusive at the bottom of the band), consistently across all three
modes.

### 8.3 `not_rated_gate`

`c_presence < 0.60` or `c_top < 0.35` or all pillars NaN → `letter = NR`, `percentile = None`,
`ci = (nan, nan)`. **Never `F`.** The score is still reported.

### 8.4 Hysteresis — default OFF

```
theta_h(L') = theta(L') + h   for every L' ranked strictly ABOVE L_prev
              theta(L') - h   for L' == L_prev
              theta(L')       for every L' below
letter = highest L' with score >= theta_h(L')
h = max(hysteresis_points (0.5), hysteresis_frac (0.15) * band_width(L_prev))
theta("A+" upward) = +inf ; theta("F" downward) = -inf
L_prev == NR  -> hysteresis is a no-op for that run
on a confirmed crossing, letter := letter_raw (NOT a single step)
```

A **full threshold re-evaluation with a directional deadband**, not a one-step walk: the source's
"move up only if `score >= theta(L_up) + h`" named only the adjacent band and was undefined for a
three-band jump (entirely plausible after a restatement or a new fiscal year), and both ends would
`KeyError` (`A+` has no higher letter; `F` is the residual band with no cutoff). This form handles
multi-band jumps, reduces to the stated rule for adjacent moves, and stays monotone in score.

**Default `enabled: false`** in the library config (D9): enabled-by-default hysteresis violates the
flat determinism guarantee on the default path and makes a nominally pure `grade()` call write to
disk. Enabled by `--hysteresis` on the CLI. Prior state is **caller-supplied** as
`previous: GradeReport | None`; `GradeStateStore` (JSON at `.stock_grader_state/grades.json`, keyed by
`(ticker, profile, scale_mode)`) is a thin CLI-side helper, keeping the library pure and testable.
A changed `pipeline_fingerprint` invalidates stored state so a stale letter never survives a
methodology change. The report carries both `letter` and `letter_raw`.

---

## 9. Layer 5 — uncertainty

### 9.1 The draw loop

```
B = 512 (config n_draws, minimum 200 enforced) ; rng per rng.py
CACHE the normalized scores once -- draws re-run ONLY the aggregation stage
for b in 1..B:
  per pillar (in sorted order):
    O_b   = leave_p_out(observed set, p, rng),  p = int(floor(0.20*n + 0.5)) for n >= 3, else 0
            (n <= 2 -> p = 0, skip, warn pillar_too_small_for_bootstrap)
    wt_b  = dirichlet_draw over O_b with kappa_metric      (WEIGHTING.md 5.6)
    s_pb  = aggregate(level1, s[O_b], wt_b)
    s_pb  = 50 + (s_pb - 50) * c_w_UNPERTURBED ** gamma    <- FIXED, not the draw's coverage
    if c_w_draw == 0 for this pillar: drop it and renormalize top-level weights over survivors
  if fewer than 2 pillars survive: DISCARD the draw
  wt_top = dirichlet_draw over surviving pillars with kappa_top
  s_b    = aggregate(level2, s_p, wt_top)
n_effective = number of retained draws ; require >= 100 else warn ci_undersampled
q_lo, q_hi = np.nanquantile(s_draws, [alpha/2, 1 - alpha/2]),  alpha = 0.05
```

`p` uses `int(floor(0.20n + 0.5))`, not `round()` — Python's `round` is banker's rounding
(`round(2.5) == 2`), a cross-implementation determinism hazard in a spec whose headline guarantee is
determinism.

**Coverage shrinkage inside the loop uses the UNPERTURBED `c_w`.** Leave-p-out is a resampling device
for estimating sensitivity, not a statement that data is genuinely missing, so it must not re-trigger
the missing-data penalty. In the source form every draw had already lost `p ≈ 0.20n` metrics, so every
draw's `c_w` was ~0.8 of the unperturbed value while `s*` used the full `c_w`: for a pillar at 90,
`50 + 40·0.8^0.5 = 85.8` in **every** draw — a systematic ~4-point downward shift for good companies
(upward for bad ones), which is a location shift, not added dispersion, and which compounds through
the level-2 geometric mean. For a genuine A+ the entire draw distribution sits below `s*`, so
`q_hi < s*`, `(q_hi − s*) < 0`, and the interval lands **entirely below the point estimate**.

### 9.2 `[F12]` CI assembly

```
s* = the UNPERTURBED pipeline run (never the bootstrap mean)
f  = min(3.0, c_top ** -0.5)
ci_lo = clip( min(s*, s* - (s* - q_lo)*f), 0, 100)
ci_hi = clip( max(s*, s* + (q_hi - s*)*f), 0, 100)
```

Containment is made **structural** by the `min`/`max`, belt-and-braces on top of the §9.1 fix.
A regression test asserts `mean(draws)` is within 0.5 points of `s*` under full coverage.

The headline score is the unperturbed run so it is reproducible and its Shapley decomposition sums
exactly. Reporting the bootstrap mean as the headline is forbidden.

Also emitted: `letter_ci = (letter(ci_lo), letter(ci_hi))` and `p_letter`, the empirical distribution
of letters across draws — the most useful single diagnostic, at zero extra cost.

### 9.3 What the interval does and does not cover

`GradeReport.interval_sources = ["weight_uncertainty", "metric_inclusion"]`. It does **not** cover
data error, normalization estimation error, or universe definition (`universe_bootstrap` defaults
**off**: it costs `O(B·m)` and conflates estimation error with a universe-definition choice). Users
will read a 95% CI as a 95% CI, so `docs/uncertainty.md` states this plainly and the field name is
available as `weight_sensitivity_interval` in the JSON output.

### 9.4 Default confidence when a method does not report one

`equal` 0.90 · `fixed`/expert 0.85 · inverse-vol / risk-parity 0.70 · PCA / eigen 0.50 · supervised
`min(0.90, |t|/4)` · 0.30 whenever the fitting panel has fewer than 250 rows · **0.40 whenever the
method fell back**, with the fallback recorded in warnings.

### 9.5 Sensitivity and counterfactual outputs

Shapley says where the score came from; users want to know what would change it. All three are cheap
given the cached normalized scores:

1. **Local elasticity** `∂composite/∂s_metric`, analytic for the mean family
   (`wt_i·(M/s_i)^(1−rho)` for the power mean), chained through both levels.
2. **Distance to next letter**: how many points of which metric move the grade to the next band.
3. **Tornado ranking** by `|elasticity × plausible_move|`, where `plausible_move` is the metric's
   own historical interquartile move.

---

## 10. Layer 6 — evaluation (`src/stock_grader/eval/`)

Without this, none of the methodological questions can be settled by anything except argument, and
the user explicitly asked for the statistics.

* `rank_ic(scores, fwd_returns)` — Spearman per date, with the HAC-corrected mean SE from
  `WEIGHTING.md` §5.4.
* `decile_spread(scores, fwd_returns)` — top-minus-bottom decile mean forward return, and the full
  decile table.
* `grade_monotonicity(letters, fwd_returns)` — is mean forward return monotone in the letter ladder;
  Spearman of letter rank against realized return.
* `hit_rate`, `auc` for the distress metrics.
* `turnover(grade_series)` — letter changes per rebalance, and weight turnover.
* `scripts/sweep_aggregators.py` — runs the fixture panel across the `rho` grid and the normalizer
  choices, producing the table that settles D11, source open-question 4 (per-metric vs per-pillar
  `k_hat`), the `(sector, size)` calibration keying question, the `universe_bootstrap` default, and
  `beta`'s band-vs-monotone encoding (`METRICS.md` §5.5).
* `scripts/eval_weighting.py` — for every registered method: OOS IC, OOS ICIR, turnover,
  `weight_confidence`. Ships a docs table so users can see how many of these methods actually beat
  equal weighting, which is the honest finding in this literature.

---

## 11. Layer 7 — profiles, graders, consensus

11 profiles (`all_weather`, `value`, `deep_value`, `growth`, `garp`, `quality`, `momentum`,
`low_volatility`, `dividend_income`, `dividend_growth`, `turnaround`) plus the `consensus` grader = 12
registered graders. Each profile fixes pillar weights, the level-2 `rho`, the normalizer stack, the
weighting method per level, and a written thesis.

`consensus_grade` runs several profiles and reports their **disagreement** as a first-class output: a
stock grading A on value and F on momentum is a fundamentally different proposition from one grading C
on both, and averaging them to the same C destroys the only interesting information in the comparison.
Profile → final combination is `level = "profile_to_final"` and reuses the whole weighting registry
(§7).

`direction_override` is a per-profile map (`{metric: ±1}`) used only for the `beta` question
(`METRICS.md` §5.5); the validator rejects an override on a `shape == "band"` metric unless the
profile also supplies a monotonic normalizer for it.

---

## 12. Naming, migration and config

### 12.1 Rename table (all via `Registry.register_alias`, `DeprecationWarning`, one minor version)

| layer | old | new |
|---|---|---|
| normalizer | `percentile` | `percentile_rank` |
| normalizer | `sigmoid_z` | `sigmoid` |
| normalizer | `piecewise` | `piecewise_linear_absolute` |
| aggregator | `weighted_mean` | `weighted_arithmetic_mean` |
| aggregator | `ces` | `power_mean_ces` |
| weighting | `entropy` | `entropy_weight` |
| weighting | `pca` | `pca_loadings` |
| weighting | `ic` | `ic_weighted` |
| weighting | `ic_ir` | `ic_ir_weighted` |
| weighting | `decorrelated` | `mahalanobis` |
| weighting | `consensus` | `robust_consensus` |
| weighting | `grunfeld` | `newey_west_tstat` |
| metric | 8 renames | `METRICS.md` §5.2 |

### 12.2 Structural migration

The repo currently has **flat** `normalize.py`, `aggregate.py`, `weighting.py`. The merged design uses
packages. In the same commit that creates `normalize/`, `aggregate/` and `weighting/`, the flat
modules are **deleted** — a package and a module of the same name on `sys.path` is ambiguous.
`pipeline.py` call sites at lines ~235 and ~268 change from
`compute_weights(X, *, method, ctx) -> Series` to `compute_weights(name, X, ctx) -> WeightResult`.

Behaviour changes to announce in the changelog because they silently alter any existing config:
`robust_z`'s MAD-zero fallback (zscore → IQR), the `[F4]` map (linear ±3σ → `Phi` CDF), the default
normalizer (`robust_z` → `winsor_rank`), the level-2 default (`ces rho` per profile → unchanged, but
cutoffs re-derived), and the not-rated sentinel.

### 12.3 Config files

| file | contents |
|---|---|
| `config/normalize.yaml` | `default: winsor_rank`; `small_universe_fallback: [historical_percentile, piecewise_linear_absolute]`; `min_universe_for_cross_section: 30`; `z_map: cdf`; per-metric overrides; band specs; stack order; `loo`; `staleness_dispersion_warn` |
| `config/aggregate.yaml` | `level1.method`, `level2.method` + `rho`, `zero_floor`, `trim_t`, `soft_min_T`, `owa_pessimism`, `choquet.gamma`, `lexicographic_gate` |
| `config/weighting.yaml` | `WEIGHTING.md` §8 |
| `config/uncertainty.yaml` | **all** of `n_draws`, `alpha`, `p_frac`, `kappa_min/max`, `universe_bootstrap`, `[F11] gamma`, `shrinkage_form`, `coverage_shrinkage`, **and both coverage gates `c_min_pillar` / `c_min_report` / `c_min_presence`** — previously split across two unrelated files |
| `config/grade_scale.yaml` | `mode: hybrid`, `beta`, the **derived** absolute table, the percentile table, hysteresis block (`enabled: false`), `pipeline_fingerprint` |
| `config/profiles.yaml` | 11 profiles + consensus membership |
| `config/metrics.yaml` | pillar membership, per-metric enable/disable, applicability overrides |
| `config/calibration/<sector>.yaml` | anchor tables, `asof`, `universe_id`, `max_age_days` |
| `config/interactions/default.yaml` | frozen Spearman matrix for the Choquet prior, with `asof` |

Every value is overridable by a CLI flag. The validator runs at load and **fails fast**: unknown
method names (with the registered list in the message), band metrics on monotonic normalizers,
`size_neutral` on `size_neutral_ok=False` metrics, redundant neutralizer stacks, `rho` outside
`[−5, 2]`, negative configured weights, inconsistent AHP matrices, configured weights above their cap,
and unconfigured metrics under `on_unconfigured: raise`.

---

## 13. Edge-case register (behaviour is defined, not discovered)

| situation | defined behaviour |
|---|---|
| Universe of one | `historical_percentile`, then the sector calibration table, then the `ALL` table, then `constant_50` with a loud warning. Warning `single_security_absolute_mode`. |
| All values identical (σ/MAD/IQR = 0) | every finite score → 50.0; never NaN, never a divide-by-zero. |
| MAD = 0 with non-zero spread | mandatory `IQR/1.34898` fallback. Without it `robust_z` returns `inf`. |
| A metric with zero observed values universe-wide | dropped from the pillar entirely (not scored 50), weight renormalized away, `metric_absent_universe_wide`. |
| All metrics in a pillar missing | pillar NaN, dropped at level 2, weight redistributed — but it contributes 0 to `c_top` (§5.2). |
| All pillars missing | composite NaN, `letter = NR`, `ci = (nan, nan)`. Never `F`. |
| Score exactly 0 into a `rho <= 0` aggregator | floored at δ; §4.3 makes this unreachable from a normalizer. |
| `rho` large and scores near 100 | log-space `logsumexp`; the constraint is cancellation, not overflow (§6.3). |
| OWA at `π ∈ {0, 1}` | branch to hard max / hard min. |
| Choquet violating monotonicity | per-pair cap → support restriction → global bisection (§6.7). |
| TOPSIS with `m == 1` | fall back to `weighted_arithmetic_mean` + warning; never compare across `universe_id`. |
| `n_g = 1` in a group wrapper | `n_min = 5` forces `λ_g = 0` long before. |
| `tau2 <= 0` in shrinkage | `λ = 0`, no neutralization. **Correct outcome, not an error path.** |
| Single sector in the universe | `< 2` groups with `n_g >= 2` → `λ = 0` + warning; **never NaN-poison the universe.** |
| Band metric → monotonic normalizer | rejected by the validator. The most likely real-world scoring bug in this system. |
| `m < 30` for cross-sectional grading | fall back to absolute + `universe_too_small_for_percentile`. |
| Hybrid thresholds tied | non-decreasing assertion + upward nudge (§8.2). |
| Score exactly on a threshold | `>=` everywhere, consistently. |
| Pillar with `n <= 2` under leave-p-out | `p = 0`, warning `pillar_too_small_for_bootstrap` — the CI understates uncertainty exactly where it is largest, so the warning matters. |
| Dirichlet α near zero | clamp `>= 0.05`, support-only sampling, resample-then-fallback. |
| Delisting in forward returns | use the delisting return (or −1.0 for a flagged bankruptcy); **never drop the row** — that is the survivorship bias `panel_is_pit` exists to prevent. |
| Horizon longer than the history | zero usable dates → supervised methods fall back, rather than silently using the few dates whose forward window happens to be complete (systematically the earliest, oldest-regime dates). |
| Date with `< 20` names | skipped **per column**, so different columns have different `T` — the NW lag and the empirical-Bayes shrinkage use each column's own `T`. |
| Two concurrent runs sharing `.cache/weights` | atomic writes (temp file in the same directory, then `os.replace`). |
| No price feed at all | `risk`/`momentum`/`liquidity` → `NOT_APPLICABLE`, out of the coverage denominator, recorded in `meta` (§5.2). |
| Pillar-drop bias | missing fundamentals are **not** missing at random (small caps, recent IPOs, foreign filers miss more). Pro-rata redistribution above `max_redistributable_share = 0.30` forces a one-notch confidence haircut, and the redistributed mass always flows into the CI width. |

---

## 14. Test invariants (the ones that must exist)

Beyond the per-layer lists in `WEIGHTING.md` §9:

**Normalization** — range containment in `[EPS, 100−EPS]`; NaN preservation; tie equality;
`test_score_of_negated_input_equals_100_minus_score`; weak LOO monotonicity;
`test_mad_zero_falls_back_to_iqr`; `test_shrinkage_single_group_no_nan`;
`test_shrinkage_lambda_limits`; `test_band_peak_is_100`; `test_sector_neutral_winsor_rank`;
`test_redundant_neutralizer_stack_rejected`; `test_piecewise_matches_winsor_rank_at_anchors`.

**Aggregation** — `test_power_mean_limits`; `test_arithmetic_equals_power_mean_rho1`;
`test_am_gm_hm_ordering` (on the common floored vector); `test_choquet_zero_interaction_equals_arithmetic`;
`test_choquet_sparse_weights_alpha_positive`; `test_wowa_reduces_to_arithmetic_and_owa`;
`test_softmin_between_min_and_mean`; `test_trimmed_mean_fractional_weights`;
`test_shapley_efficiency_all_pointwise_aggregators`; `test_shapley_efficiency_under_coverage_shrinkage`;
`test_nested_contributions_sum_to_composite_minus_50`; `test_nan_weight_renormalization`;
`test_zero_weight_vector_vs_no_observed_weight`.

**Grade + uncertainty** — `test_hybrid_reduces_to_pure_modes` (letter **set** identity);
`test_thresholds_non_decreasing`; `test_hysteresis_blocks_small_flip`;
`test_hysteresis_multi_band_jump`; `test_hysteresis_top_and_bottom_letters`;
`test_low_coverage_gives_NR_not_F`; `test_four_of_five_pillars_absent_gives_NR`;
`test_missing_pillar_is_not_better_than_bad_pillar`; `test_price_free_grade_is_not_NR`;
`test_point_estimate_inside_ci`; `test_draw_mean_close_to_point_estimate`;
`test_same_seed_same_ci` (including a shuffled input-dict order, to catch iteration-order dependence);
`test_letter_probabilities_sum_to_one`; `test_coverage_shrinkage_applied_once`.

**Metrics** — `test_every_metric_has_unique_snake_case_name`; `test_band_iff_direction_zero`;
`test_not_applicable_never_penalised`; `test_reciprocal_pairs_absent_from_catalog`;
`test_altman_and_piotroski_use_absolute_normalizer`;
`test_composites_excluded_from_correlation_panel`.

---

## 15. Remaining open questions (explicitly not decided here)

1. **Does the DataProvider deliver point-in-time universe membership including delisted names?** If
   not, `panel_is_pit` is permanently `False` and all six supervised methods are dead code that falls
   back on every run. This is the single biggest unresolved dependency and it belongs to the data
   area. Until answered, the shipped ensembles are viable only because composites are exempt from
   walker gating (`WEIGHTING.md` §4).
2. **Canonical `(horizon_days, rebalance_spacing_days)` for the shipped fixtures.** Every NW lag,
   embargo width and `min_dates` threshold is parameterized on these; `(63, 21)` is an assumption and
   must be confirmed against what the fixture CSVs actually contain. Read from config, never
   hard-coded.
3. **Does the reference panel ship in the repo** (megabytes, goes stale, but makes single-stock grades
   far better) **or is it generated into `.cache` on first use?** Assumed: a bundled dated CSV under
   `data/panels/` with a 180-day staleness limit. The data area owns this.
4. **`universe_bootstrap` default for large universes.** It captures a genuinely real source of
   uncertainty the current CI ignores entirely — the cross-sectional normalization is itself estimated
   — so the shipped intervals are known to be somewhat too narrow. `eval/` decides.
5. **`(sector, size_bucket)` calibration keying** (§4.6) and **per-metric vs per-pillar `k_hat`**
   (§4.8). Both are now empirical questions with a harness to answer them.
6. **Level-2 sector-conditional pillar weights.** The context carries sector labels, so a
   sector-conditional `fixed` table is a small extension, but it multiplies the config surface and the
   calibration burden. Supported in the schema, unused by the shipped profiles.
