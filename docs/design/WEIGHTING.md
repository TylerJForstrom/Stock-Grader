# WEIGHTING.md — weighting methods: exact algorithms, preconditions, fallbacks

**Status:** normative. Companion to `SPEC.md` (layer architecture) and `METRICS.md` (catalog).
This is the defining feature of the product: **33 interchangeable weighting methods**, applied at
both aggregation levels (metric → pillar, pillar → final), and ensembled.

---

## 0. Contract

```python
class WeightingMethod(Protocol):
    name: str
    needs_panel: bool
    needs_returns: bool
    min_n: int              # minimum panel ROWS (securities)
    good_n: int             # rows at which confidence reaches 1.0
    min_dates: int          # minimum historical dates (supervised only)
    allows_negative: bool   # may EMIT negatives; the boundary still projects them (§0.4)
    is_composite: bool      # runs other methods; exempt from walker-level gating
    requires_dispersive_normalizer: bool
    stage_views: tuple[str, ...]   # which panel views it may read
    fallback: str | None
    def fit(self, X: pd.DataFrame, *, meta: WeightingContext) -> pd.Series: ...
```

`compute_weights(name, X, ctx) -> WeightResult` is the **only** public entry point. It never returns
a bare `Series`.

```python
@dataclass(frozen=True)
class WeightResult:
    weights: pd.Series          # index == ctx.columns, sums to 1.0 exactly, all >= 0
    method_used: str
    method_requested: str
    fallbacks: list[WeightFallback]
    confidence: float           # [0,1] — REQUIRED, consumed by the Uncertainty layer
    fallback_used: bool
    dispersion: pd.Series | None    # per-column sigma_w, when a meta method produced it
    diagnostics: dict           # turnover, n_eff, effective weights, capped pairs, ...
```

### 0.1 What `X` is

`X` **is** `ctx.panel` restricted to the columns that survived the pre-pipeline: an
`n_securities × k_columns` frame of already-direction-adjusted 0..100 normalized scores, in
`ctx.columns` order. For a single-stock grade it is a 1-row frame. There is no other reading; every
method may assume it. `ctx.panel` is the unfiltered original and is used only for diagnostics.

### 0.2 Decorator registration, Protocol dispatch

Methods are written as plain functions and registered with the **existing shared registry**
(`stock_grader.registry.WEIGHTINGS`) — the layer does not define a second decorator. The registry
wraps each function in a small adapter exposing `.fit(X, *, meta)` plus the metadata attributes, so
Protocol dispatch and function authoring coexist.

### 0.3 One fit per (level, group, asof) — never per target

**Resolution of a direct conflict between the two reviews.** Weights are fitted **once per
(level, group, asof) over the whole universe**, on the full canonical column set that survived
*universe-level* filtering. Per-target availability is applied afterwards as a **pure restriction +
renormalization**, never a refit.

* Universe-level filtering (pre-pipeline, §2): panel coverage, zero-variance columns, `dedup_corr`,
  `k == 1` short-circuit. These change the fitted problem, so they happen **before** `fit`.
* Target-level filtering (post-pipeline, §3): `target_has[j] == False` → zero and renormalize.

This gives both properties the reviews demanded: statistical methods fit the reduced problem rather
than a truncation of a larger one, *and* two securities graded the same day against the same
universe share identical relative weights on the metrics they both have — without which the reported
cross-sectional percentile compares scores built on different yardsticks. It also makes the
`shapley` / `gradient_boosting_importance` cache key target-independent, which is the only thing that
makes those two methods affordable at all.

### 0.4 Negative weights — resolved: long-only everywhere

`config.allow_negative_weights` is **False unconditionally at both levels** and is not exposed as a
user flag. Methods that naturally produce signed coefficients (`mahalanobis`, `regression` with
`non_negative=False`) declare `allows_negative=True`, and the boundary in `apply_post_pipeline`
projects: `v := max(v, 0)`, warning `negative_weights_projected` with the original vector written to
`diagnostics["signed_weights"]`.

Rationale, and why this overrides the shared contract's "≥ 0 unless the method says otherwise": the
contract also fixes `PillarScore.score` in `[0, 100]`. With a negative `w_j`, `sum_j w_j s_j` escapes
that range, `water_fill` is undefined (documented for `v >= 0`), renormalizing by `sum(w)` explodes
as `sum(w) → 0` and inverts the grade when `sum(w) < 0`, `ln(w_j)` in the power mean is undefined,
`Dirichlet(κw)` raises, and Choquet monotonicity fails. Signed weights are retained as **diagnostic
output only**. Additional guard in the projection: if `sum(v) < 0.5 · sum(|v|)` the vector is
mostly cancellation — refuse and walk the fallback chain with `negative_weight_degenerate`.

### 0.5 The weighting layer does not compute scores

`post_pipeline` returns weights and diagnostics. It does **not** compute `S = Σ w_j s_j` and does
**not** compute contributions. Both belong to the Aggregator, which under the default configuration
is *not* a weighted arithmetic mean (level 2 defaults to `power_mean_ces(rho=0)`), so `w_j · s_j` is
not the contribution and the `W_pillar · w_j · s_j` trace identity holds only for
`weighted_arithmetic_mean` at both levels. Contributions come from `aggregate/contributions.py`
(SPEC §6.5).

---

## 1. `WeightingContext`

`@dataclass(frozen=True, eq=False)` — `eq=False` is required: the default `eq=True` synthesises
`__hash__` from all fields, and the fields include DataFrames, so any hashing (which
`functools.lru_cache` on a method performs implicitly) raises `TypeError: unhashable type`. Views are
cached in a private dict installed by `__post_init__` via `object.__setattr__`, **not** with
`functools.cache` (which would pin every context object in a module-level cache for the process
lifetime). No `slots=True` — it is incompatible with `cached_property`.

| field | type | notes |
|---|---|---|
| `level` | `'metric_to_pillar' \| 'pillar_to_final' \| 'profile_to_final'` | third level added; the profile ensemble is structurally the same problem and reuses the whole registry |
| `group` | `str` | pillar name, or `'__final__'`, or `'__profiles__'` |
| `columns` | `list[str]` | canonical order; defines the returned index |
| `asof` | `pd.Timestamp` | |
| `panel` | `pd.DataFrame \| None` | n × k normalized 0..100 scores at `asof` |
| `panel_history` | `dict[Timestamp, DataFrame] \| None` | |
| `fwd_returns`, `fwd_returns_history` | | |
| `horizon_days`, `rebalance_spacing_days` | `int` | defaults 63 / 21, **overridden by whatever the fixtures contain** — read from `config/weighting.yaml`, never hard-coded |
| `sectors`, `sector_of_target` | | consumed by `size_neutral` fixed effects and sector-conditional `fixed` |
| `prior_weights` | `pd.Series \| None` | |
| `previous_weights` | `pd.Series \| None` | previous rebalance's weights — the input to `prior` and to turnover smoothing |
| `coverage` | `pd.Series` | per-column non-null fraction. **Defaults to `pd.Series(1.0, index=columns)` when `panel is None`** — without this default the single-stock path dies in the mandatory wrapper before any method runs |
| `applicable` | `pd.Series[bool]` | per column: is this metric applicable to the target's sector class at all |
| `target_has` | `dict[str, bool]` | |
| `panel_is_pit` | `bool` | hard-gates every `needs_returns` method |
| `panel_history_normalized_pit` | `bool` | **also** hard-gates them: historical scores must have been normalized *within each historical date against that date's universe*. If today's normalizer was run over back-filled raw data, every IC in the system is inflated by full-sample winsorization quantiles |
| `reporting_lag_applied` | `bool` | `False` → `LookAheadError` at construction, not at fit time |
| `normalizer_name`, `normalizer_is_rank_preserving` | `str`, `bool` | §4.0 |
| `directions` | `dict[str,int]` | **direction is applied upstream; no method may read this to adjust a sign.** It exists solely so `negative_ic:{col}` and `possible_mis_signed:{col}` warnings can name the declared direction |
| `rng` | `np.random.Generator` | §1.1 |
| `warnings` | `WarningSink` | a separate mutable object held by reference — not a mutable field on a frozen dataclass |
| `config` | `dict` | |
| `fallback_depth` | `int` | |

Cached views: `unit_panel()`, `z_panel()`, `raw_panel()`, `cov(view)`, `corr(view, shrunk: bool)`,
`reference_panel()`.

`dataclasses.replace(ctx, panel=reference_panel, ...)` **must also recompute `coverage` and
`target_has` against the substituted panel** — the fallback walker does this explicitly.

### 1.1 Deterministic seeding

```python
def stable_hash(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")

seed = stable_hash(f"{ticker}|{asof}|{profile}|{universe_id}|{pipeline_fingerprint}|{global_seed}")
rng  = np.random.default_rng(np.random.SeedSequence([seed, stable_hash(level), stable_hash(group)]))
```

Never `hash()`: Python string hashing is salted per process (`PYTHONHASHSEED`), so two runs of the
same CLI command get different RNG streams — and `hash()` returns negative integers, which
`SeedSequence` rejects outright, so roughly half of all `(level, group)` pairs would crash at context
construction. This module (`src/stock_grader/rng.py`) is shared with the Uncertainty layer; there is
exactly one seeding implementation in the codebase.

Determinism additionally requires **fixed iteration order**: pillars, metrics and members are
iterated in `sorted(name)` order everywhere an RNG is consumed, and every sort of scores carrying
weights uses `np.argsort(kind="stable")` with the column name as the documented secondary key
(`weighted_median`, `trimmed_mean`, `owa`, `wowa`, `hrp` bisection). Verified by a **two-subprocess**
test with `PYTHONHASHSEED=random` diffing the JSON report byte-for-byte; an in-process double-call
cannot detect the `hash()` class of bug.

---

## 2. Pre-pipeline (before `fit`)

Runs once per `(level, group, asof)`.

1. **Applicability drop.** Columns with `applicable[j] == False` for the whole universe are removed
   and recorded as `NOT_APPLICABLE` — they leave the coverage denominator entirely (SPEC §5.2).
2. **Zero-variance drop.** Any column with `std == 0` on the unit panel is removed before fit. This
   is a *shared* step, not a per-method note: `pca_loadings`, `mahalanobis`, `min_variance`,
   `max_diversification`, `risk_parity` and `hrp` all break on it, in four different ways.
3. **Panel-coverage split.** Columns with `coverage[j] < min_panel_coverage (0.60)` are **excluded
   from the statistical fit but not zeroed** — see §3 step 2 for what they receive.
4. **`dedup_corr`** if enabled for this level (§8.1).
5. **`k == 1` short-circuit.** Return `Series([1.0])` without dispatching. `entropy_weight`'s
   `k_ent = 1/ln(n)` and `ahp`'s consistency index are undefined at `k == 1`.
6. **`k == 0`** (everything dropped): `PillarScore is None`; the pillar is removed at level 2 and its
   weight redistributed — but its coverage contribution is 0, which is what makes the corrected
   `c_top` fire (SPEC §5.2).

### 2.1 NaN policy

Columns below `min_panel_coverage` are excluded from estimation. Remaining NaN cells are imputed
with the cross-sectional median of that column **at the same date within the same sector** if the
sector has ≥ 10 names, else the whole-date median. `imputed_fraction_j` is recorded;
`heavy_imputation:{col}` warns above 0.25.

Never fill with 0 — in a 0..100 score panel 0 means "worst in universe", not "unknown".
Never forward-fill across dates — that is look-ahead when the later value did not exist at `asof`.

`critic` additionally refuses any *pair* whose **observed** (pre-imputation) overlap is below 30
names, because median imputation biases `r` toward 0 and CRITIC rewards low `r` with *more* weight —
sparse columns would be systematically over-weighted. Records `pair_overlap_min` in diagnostics.

---

## 3. Post-pipeline (after `fit`) — `apply_post_pipeline(v, ctx, *, allows_negative, tau, apply_shrink)`

1. **Target restriction.** `target_has[j] == False` → `w_j := 0`. Pure restriction; no refit.
2. **Low-coverage columns.** A column excluded from the fit in pre-pipeline §3 but which the target
   *does* have is assigned `min_j(w_j)` over the fitted columns (not 0). **This resolves a direct
   contradiction between the two source specs**; the nan_policy behaviour wins because zeroing a
   metric the target actually has throws away information, and because zeroing on *panel* coverage
   means the same stock graded against two universes silently uses two different metric sets.
3. **Sign policy.** §0.4.
4. **Cap / floor** by water-filling (§3.1).
5. **Prior shrink** — only when `apply_shrink=True` (§3.2).
6. **Cap re-check.** Re-run step 4 once if shrinkage pushed anything above the cap; accept the
   residual rather than iterating to a fixed point that may not exist.
7. **Exact renormalization.** `w /= w.sum()`, then `w[argmax w] += 1.0 - w.sum()` to force
   `sum == 1.0` in float64.
8. Reindex to `ctx.columns` (missing → 0).

### 3.1 Water-filling cap/floor — corrected algorithm

The source spec's loop recomputed `w = v / sum(v)` over **all** coordinates each sweep while writing
the clamped values only to `w`, never to `v`. Previously-fixed coordinates therefore re-entered the
free pool, the iteration oscillated to the 50-sweep cap and returned a vector violating the caps —
while `test_respects_caps_and_floors` was advertised as passing. Replaced with the monotone
growing-fixed-set algorithm, which terminates in at most `k` rounds:

```
feasibility (computed on the POST-DROP k, never at config load, because k is data-dependent):
    if k * wmax < 1.0:  wmax = 1.0            ; warn cap_infeasible
    if k * wmin > 1.0:  wmin = 1.0 / k        ; warn floor_infeasible
    if |k*wmax - 1| < 1e-12 or |k*wmin - 1| < 1e-12:  return full(k, 1/k)   # feasible set is one point

Fixed_hi, Fixed_lo = set(), set()
for _ in range(k + 1):
    R = 1.0 - len(Fixed_hi)*wmax - len(Fixed_lo)*wmin
    H = free set
    if not H or sum(v[H]) <= 0:  distribute R equally over H; break
    w[H] = v[H] * R / sum(v[H])
    newly_hi = {j in H : w_j > wmax + 1e-12}
    newly_lo = {j in H : w_j < wmin - 1e-12}
    if not newly_hi and not newly_lo: break
    Fixed_hi |= newly_hi ; Fixed_lo |= newly_lo
assemble w from the fixed bounds plus w[H]
assert max(w) <= wmax + 1e-9 and min(w) >= wmin - 1e-9
```

Defaults `wmin = 0.0`; `wmax = 0.40` at metric level, `0.50` at pillar level.

**`fixed` and `ahp` at level 2 are exempt from the cap.** A `value` profile whose identity is
"valuation gets 55% of the grade" must not have that silently clipped to 0.50 — the cap exists to
bound *statistical* overfitting, not to override an analyst's explicit configuration. Whenever any
configured weight is reduced by capping, `analyst_weight_capped:{group}:{configured}->{capped}` is
emitted and config-load validation flags it.

### 3.2 Shrink-to-prior — applied exactly once

`w_final = (1 − τ)·w + τ·w_prior_s`

where **`w_prior_s` is the prior restricted to the surviving columns and renormalized**:
`w_prior_s = w_prior[surv] / w_prior[surv].sum()`, falling back to uniform-over-survivors if that sum
is ≤ 0. Without the renormalization the effective shrinkage intensity is `τ · Σ_surv w_prior`, an
arbitrary number below `τ` that varies by ticker — a stock missing three metrics would get materially
*less* prior anchoring than one missing none, exactly backwards.

Defaults: `τ = 0.00` a-priori/unsupervised, `0.25` supervised, `0.40` `gradient_boosting_importance`.

**Members of a composite method run with `apply_shrink=False`** (caps and sign policy only); the
outer combiner runs with `apply_shrink=True` and `τ = Σ_a m_a·τ_a`, the membership-weighted τ.
Otherwise a supervised member is shrunk at 0.25 individually and then again at the ensemble level,
making the amount of prior anchoring a function of arbitrary nesting depth.

### 3.3 Turnover control

Optional, off at level 1, **on for supervised methods**: `w_t = (1−φ)·w_t^fit + φ·w_{t−1}` with
`φ = 0.50`, applied before step 7, using `ctx.previous_weights`. Always report
`weight_turnover = 0.5·Σ_j |w_j(t) − w_j(t−1)|` in `diagnostics`. HRP dendrograms, PCA loadings and
elastic-net supports all flip discontinuously under small data perturbations, and the letter grade is
a thresholded function of the composite — without smoothing a stock oscillates B+/A− with no news.

---

## 4. Fallback walker

```
resolve(name, ctx):
    m = registry[name]
    if m.is_composite:                     # EXEMPT from precondition gating
        return m                           # members walk their own chains
    if m.requires_dispersive_normalizer and ctx.normalizer_is_rank_preserving:
        fallback(reason="dispersion_degenerate_under_rank_normalizer")
    if m.needs_returns and (not ctx.panel_is_pit
                            or not ctx.panel_history_normalized_pit
                            or ctx.fwd_returns_history is None
                            or T < m.min_dates):
        fallback(reason="supervised_preconditions_unmet")
    if m.needs_panel and (ctx.panel is None or n_rows < m.min_n):
        try ctx.reference_panel()          # bundled, panel_is_pit=False -> unsupervised only,
        ...                                # rejected if age > 180 days
        else fallback(reason="panel_too_small")
    each hop: append WeightFallback(...); fallback_depth += 1
    terminate at 'equal' if fallback_depth > 4 or a name repeats (cycle)
```

**Composite exemption is a must-fix, not a nicety.** With `needs_returns = OR over members`, an
ensemble containing one supervised member declares `needs_returns=True`; on a non-PIT provider the
walker refuses the *entire ensemble*, falls through to `robust_consensus` (also `True` under the OR
rule) and lands on plain `equal` — discarding the `critic` and `hrp` members that would have run
perfectly well, and collapsing the shipped production default to equal weighting on every run.
Composites therefore declare `needs_panel=False, needs_returns=False, min_n=1, min_dates=0,
is_composite=True` and call `compute_weights` per member internally.

**Registry self-test at import (`validate_registry()`)** walks the union of **fallback edges and
configured `members:` edges**, and raises on any cycle or dead end. `robust_consensus.fallback` is
`'equal'`, **not** `'ensemble'` — the source spec had `ensemble → robust_consensus → ensemble`, a
two-node cycle that would make the mandated self-test raise at import and the package fail to load.
`run_members` additionally carries a depth counter refusing any member already on the resolution
stack, since a config can name `ensemble` inside `ensemble.members` and no static check sees that.

### 4.0 Normalizer dependency (the headline finding of both weighting reviews)

Under a **rank-preserving** normalizer (`percentile_rank`, `gaussian_rank`, `quantile_bucket`,
`rank_minmax`) every panel column is a permutation of the same marginal distribution, so std,
variance, range and Shannon entropy are **identical across columns by construction**:
`stddev`, `inverse_variance`, `entropy_weight` and `coefficient_of_variation` all return exactly
`1/k`, and `critic` collapses to a pure conflict score. Under the previous default `robust_z` (a
median/MAD z linearly saturating at ±3σ) the only thing that differs between columns is tail mass
beyond 3 MADs, so `stddev` weights metrics by how fat-tailed their raw distribution is.

Three mitigations, all required:

1. `requires_dispersive_normalizer = True` on those five methods; the walker hard-refuses under a
   rank normalizer with `dispersion_degenerate_under_rank_normalizer` rather than returning equal
   weights that look like a real fit.
2. A runtime detector regardless of the declared normalizer: if
   `max_j σ_j / min_j σ_j < 1.10` on the unit panel, emit `dispersion_uninformative` and fall back.
3. `ctx.raw_panel()` — robust-scaled **pre-normalization** metric values (`value / MAD_j`,
   winsorized at 2.5/97.5) — is the correct view for dispersion weighting, and is the declared view
   for `stddev` and `inverse_variance`.

`entropy_weight` is consequently **dropped from the shipped default level-1 ensemble** and its
allocation moved to `critic` and `hrp`, which retain correlation information under any monotone
normalizer.

### 4.1 Confidence

```
confidence = 0.6 ** fallback_depth
           * clip((n_used - min_n) / max(good_n - min_n, 1), 0, 1)
           * (0.85 if unsupervised fit on a non-PIT panel else 1.0)
```

The source spec's `min(1, n_used / min_n)` gave **confidence 1.0 at exactly the minimum sample** —
full confidence on pure noise. The interpolation between `min_n` and `good_n` gives ~0 at the floor.
Supervised methods additionally multiply by `1 − p_perm` where `p_perm` is the permutation-null
p-value (§6.0).

---

## 5. Shared numerics

### 5.1 Panel views

```
winsorize:  X'_ij = clip(X_ij, q_j(0.025), q_j(0.975))     # this date's own quantiles
unit_panel: rng_j = max_i X'_ij - min_i X'_ij
            U_.j = 0.5                      if rng_j < 1e-12      # EXPLICIT BRANCH, not a floor
                   (X'_.j - min_j) / rng_j  otherwise
z_panel:    Z_ij = (X'_ij - mu_j) / sigma_j,  sigma_j floored at 1e-8,  ddof=1
raw_panel:  R_ij = clip(raw_ij, q(0.025), q(0.975)) / (1.4826 * MAD_j)
```

The degenerate branch must be explicit: the source spec said "denominator floored at 1e-12 → column
becomes all 0.5", but the arithmetic gives `0 / 1e-12 = 0.0`, not 0.5. 0.5 is the deliberate neutral
choice, consistent with `normalize._neutral`.

### 5.2 Ledoit–Wolf shrinkage (Chen et al. form)

```
S       = Y^T Y / (n - 1)                    # ddof=1 EVERYWHERE — see note
mu      = trace(S) / k
if mu < 1e-14:  raise CovarianceDegenerate   # all-constant columns -> Sigma would be the ZERO matrix
d2      = ||S - mu*I||_F^2
b2      = min( (1/n^2) * sum_t ||y_t y_t^T - S||_F^2 , d2 )
delta   = 1.0 if d2 <= 1e-12 * k * mu**2 else b2 / d2      # SCALE-RELATIVE threshold
delta   = clip(delta, 1e-3, 1.0)                            # positive floor
Sigma   = delta*mu*I + (1-delta)*S ; Sigma = 0.5*(Sigma + Sigma^T)
if cond(Sigma) > 1e8:  Sigma = nearest_pd(Sigma, ridge sized to bring cond below 1e8)
                       warn covariance_ill_conditioned
if delta >= 0.999:     warn covariance_fully_shrunk        # Sigma ~ mu*I: every risk method
                                                           # collapses to equal/inverse-vol
```

Three corrections over the source spec. (a) The `d2 < 1e-18` threshold was **absolute** and therefore
scale-dependent — on the unit panel `d2` legitimately sits near 1e-4..1e-6 and the branch would never
fire; made relative. (b) `mu == 0` (every column constant) produced `Sigma = 0·I`, the zero matrix,
and an **uncaught** `LinAlgError` from `cho_factor`; now raised as a typed exception every consumer
catches and converts to a fallback. (c) `S` used `ddof=0` while `stddev`/`inverse_variance` use
`ddof=1`, so `diag(Sigma) != sigma^2` and `test_risk_parity_diagonal_equals_inverse_volatility` would
fail by `sqrt(n/(n-1))`; `ddof=1` is now used everywhere.

The honest justification for making shrinkage mandatory: **it bounds the condition number**, and the
`delta >= 1e-3` floor plus the `nearest_pd` guard is what guarantees a reproducible unique optimum.
The source spec's claim that shrinkage alone "restores strict convexity and therefore uniqueness" is
true but not load-bearing — with `n >> k`, `delta` is data-driven and can be ~1e-4, at which point
`SLSQP` at `ftol=1e-12` lands on different points along the near-flat direction across BLAS builds.

Every `cho_factor` / `eigh` consumer wraps in `try/except LinAlgError -> fallback`.

### 5.3 Box-constrained simplex projection

Projection onto `{w : Σw = 1, wmin ≤ w ≤ wmax}` by bisection on `θ`: `g(θ) = Σ_j clip(u_j − θ, wmin,
wmax) − 1` is continuous and monotone decreasing, so 60 bisection steps over
`[min(u) − wmax, max(u) − wmin]` reach machine precision.

This **replaces** the plain Duchi simplex projection in the `min_variance` backup solver. The primary
`SLSQP` path enforces box bounds while Duchi ignores `wmax` entirely, so the two solvers optimized
different feasible sets and `test_min_variance_slsqp_matches_projected_gradient` failed exactly when
a cap binds — at `wmax = 0.40` with 3–5 metrics per pillar, the common case rather than a corner.
Acceptance now checks **both** `min(w) >= wmin - 1e-9` **and** `max(w) <= wmax + 1e-9`.

### 5.4 Newey–West HAC standard error of a mean

```
abar = mean(a) ; gamma_l = (1/T) * sum_{t=l+1..T} (a_t - abar)(a_{t-l} - abar)
L    = min( max(ceil(h/spacing) - 1, floor(4*(T/100)**(2/9))), max(1, T // 4) )
se^2 = (1/T) * [ gamma_0 + 2 * sum_{l=1..L} (1 - l/(L+1)) * gamma_l ]
if gamma_0 <= 0:  t := 0 ; warn constant_ic_series
if uncapped L exceeded T//4:  warn hac_lag_truncated  AND fall back
```

Two corrections. (a) The Bartlett kernel is positive semi-definite, so `se² < 0` is **impossible** —
both source guards ("possible with Bartlett on short samples") rest on a false premise. The real
degenerate case is `gamma_0 == 0` for a constant series, handled separately. (b) `L` had **no upper
bound**: with `T = 24` and a long horizon, `L ≥ T` makes the `gamma_l` sum range empty and the
estimator silently degenerates to the *uncorrected* `gamma_0/T` — i.e. the overlap correction the
spec calls mandatory switches itself off exactly when overlap is most severe. Truncation now warns
and refuses.

`statsmodels` cross-check: `cov_kwds={'maxlags': L, 'use_correction': False}` so it matches this hand
formula exactly. With `use_correction=True` statsmodels multiplies by `T/(T−1)` — 2.1% at `T = 24`,
enough to fail `test_newey_west_se_matches_statsmodels` at any sane tolerance. The test asserts
equality to 1e-10, not a loose tolerance, because a loose tolerance hides exactly this bug.

### 5.5 Purged forward-chaining CV

Split sorted unique dates into **`n_splits + 1 = 6`** equal blocks; blocks 2..6 are the five
validation folds, so **fold 1 has a non-empty training set** (block 1). Training set for fold `f` =
all dates `t` with `t + h_days ≤ min(val_dates)` — the embargo is expressed in **calendar terms**,
not as a fixed count of index positions, because rebalance spacing is not uniform once holidays
intervene. Require `len(train) ≥ max(8, 2·e)` per fold; drop folds that fail with
`cv_folds_reduced:{n}`; if fewer than 3 folds survive, refuse and fall back with
`insufficient_cv_folds`.

The source definition ("train = all dates strictly before `min(val)`") gave fold 1 an **empty**
training set whenever the 5 blocks tile the whole range — `ElasticNet.fit` and `HistGBR` both raise
on a zero-row design, and a silent skip would leave the CV running on 4 folds while claiming 5, with
`alpha` selection biased toward the shortest-history regimes.

Never random k-fold, never shuffle: both leak the future twice (shuffled dates, and overlapping
forward-return windows straddling the split), and will select an `alpha` far too small.

### 5.6 Dirichlet weight uncertainty (consumed by the Uncertainty layer)

```
support = {j : W_j > 1e-9}          # sample ONLY over the support; numpy raises on alpha <= 0,
                                    # and zeros are the norm after coverage drops and projection
sigma_floor = 0.25 * (1 - confidence) / k
sigma_w = maximum(sigma_w, sigma_floor)
alpha_0 = mean_j[W_j (1 - W_j)] / mean_j[sigma_w,j^2] - 1
alpha_0 = alpha_0 * confidence**2                       # agreement-by-degeneracy is not confidence
alpha_0 = clip(alpha_0, max(k, 1.0 / max(min_j W_j, 1e-6)), 1e4)   # forces every alpha_j >= 1
alpha_j = max(alpha_0 * W_j, 0.05)
draw; if any non-finite or sum <= 0: resample up to 5x, then hold wt fixed
      with warning dirichlet_draw_degenerate
```

The algebra (`Var = W(1−W)/(α₀+1)`) is correct; every fix here is a guard. The critical one is the
`confidence²` factor: when every member falls back to `equal` — the *least* informed case, and
exactly what happens for a single-stock grade — `sigma_w` is identically 0, the division yields `inf`
clipped to 10 000, and the system reports its **tightest** confidence interval on its **weakest**
grade. The `alpha_j >= 1` clip prevents U-shaped Beta marginals that make sampled weights bounce
between 0 and ~1.

---

## 6. Method catalog

Notation for each entry: **needs_panel / needs_returns / min_n / good_n / min_dates / allows_negative
/ fallback**. `COLL:` records how the method handles collinear (near-duplicate) columns — the single
most consequential difference between these methods.

### 6.0 Supervised gates (apply to §6.4, §6.5)

Every `needs_returns` method is hard-gated on `panel_is_pit` **and**
`panel_history_normalized_pit`, and additionally runs a **permutation null**: shuffle forward returns
*within date* 200 times, record the distribution of the selected model's out-of-fold IC, report the
empirical p-value in `explain`, and refuse the fit (fall back) if `p > 0.10`. With `k = 10–15`
metrics, `T = 24` dates and a 36-point hyperparameter grid selected on OOF IC, the selected
configuration's OOF IC is itself an upward-biased estimate, and nothing else in the design corrects
for it.

All supervised methods apply **exponential time decay** with a configurable half-life (default 3
years) to the per-date ICs and as `sample_weight` on the pooled regression. Equal-weighting a date
from five years and two regimes ago against last quarter makes every supervised weight structurally
stale.

Every supervised method carries `max_seconds` (default 30 at grade time); on timeout it falls back
with `method_timed_out` rather than blocking the CLI with no output.

---

### 6.1 A-priori family (needs no data — these are what keep a single-stock grade alive)

#### `equal`
`w_j = 1/k`.
`False / False / 1 / 1 / 0 / False / None (terminal)`.
The universal terminal. `COLL:` not handled — two duplicates give the concept `2/k`. Mitigate with
`dedup_corr`.

#### `fixed`
`w_j = c_j / Σ_l c_l` from `config['weights'][level][group]`.
`False / False / 1 / 1 / 0 / False / equal`.
Negative config entries raise `ConfigError` at **load**. Keys not in `ctx.columns` →
`unknown_weight_key`, ignored. `Σc <= 0` → fall back.
**`on_unconfigured` policy (new):** a column in `ctx.columns` with no config entry emits
`unconfigured_metric:{col}` and is handled per `on_unconfigured ∈ {raise, warn_and_zero,
equal_share}`, default `raise` in CI and `warn_and_zero` at runtime. The source spec's silent
`default_weight = 0.0` meant every newly registered metric was invisibly excluded from every profile
until someone remembered to edit the YAML, and the only defined warning fired for the opposite case.
A registry self-test asserts every registered metric appears in every shipped profile's table.
Default level-2 method for all profiles. Sector-conditional tables are supported via
`weights[level][group][sector]` and fall back to the sector-agnostic table.

#### `prior`
`w = ctx.previous_weights` if present, else `ctx.prior_weights`, else `equal`; renormalized over
survivors.
`False / False / 1 / 1 / 0 / False / fixed → equal`.
Defined here because the source spec listed it in the public API with no entry, and because it is the
natural home for turnover smoothing (§3.3).

#### `ahp`
Saaty AHP: principal eigenvector of an analyst reciprocal pairwise matrix.
`False / False / 1 / 1 / 0 / False / fixed → equal`.

Matrix `A` is indexed **by column name in YAML** (a dict of dicts), never a positional upper triangle
— positional indexing silently misaligns when the surviving column set differs from the YAML order.
Extract the submatrix over surviving columns and **recompute the eigenvector on the submatrix**; do
not renormalize the full-`k` weights, which is equivalent only for a perfectly consistent `A`.

```
entries clipped to [1/9, 9]; assert A entrywise positive and reciprocal to 1e-9
power iteration: v0 = 1/k ; v_{t+1} = A v_t / ||A v_t||_1 ; stop at ||Δ||_inf < 1e-12 or 1000 iters
lambda_max = (1/k) * sum_i (A w)_i / w_i
CI = (lambda_max - k) / (k - 1)
RI (Saaty 1980): 1:0.00 2:0.00 3:0.58 4:0.90 5:1.12 6:1.24 7:1.32 8:1.41 9:1.45
                 10:1.49 11:1.51 12:1.48 13:1.56 14:1.57 15:1.59
n > 15: Alonso-Lamata (2006)  CR = (lambda_max - n) / (2.7699*n - 4.3513 - n)
CR = CI / RI  for n <= 15 ; k = 2 -> CR := 0 (a 2x2 reciprocal matrix is always consistent)
reject if CR > 0.10
```

**RI lookup uses the reduced `n`.** If the submatrix `CR > 0.10` while the full `CR` was fine, warn
`ahp_submatrix_inconsistent` and fall back to `fixed` — do not raise at grade time.
`on_inconsistent='raise'` applies **at config load**, where an inconsistent matrix is a config bug
that must fail fast. `repair` mode: replace the entry with the largest deviation `d_ij = a_ij w_j /
w_i` by `w_i/w_j` (and its reciprocal), recompute, up to 5 times.
`COLL:` none — AHP never sees data, so duplicates are double-counted exactly as specified.

#### `rank_order_centroid`, `rank_sum`, `rank_reciprocal` *(new — ordinal a-priori family)*
Given an analyst **ordering** only (no magnitudes), rank `r_j ∈ 1..k` best-first:

```
rank_order_centroid: w_j = (1/k) * sum_{i=r_j..k} 1/i          # ROC, the standard choice
rank_sum:            w_j = (k - r_j + 1) / (k(k+1)/2)
rank_reciprocal:     w_j = (1/r_j) / sum_l (1/r_l)
```

`False / False / 1 / 1 / 0 / False / equal`.
Added because the source spec covered only AHP, which demands a full `k × k` matrix and a consistency
audit, while omitting the entire cheap ordinal family analysts actually use — and these need only an
ordering, are deterministic one-liners, and directly serve the headline single-stock scenario. Ties
in the ordering share the averaged weight of their rank block.

---

### 6.2 Dispersion family — **all require a dispersive normalizer (§4.0)** and read `raw_panel()` or `unit_panel()`

#### `entropy_weight`
Shannon-entropy MCDM: a column that discriminates strongly (low entropy) earns more weight.
```
on unit_panel U:  p_ij = U_ij / sum_i U_ij   (column sum 0 -> e_j := 1)
k_ent = 1 / ln(n)
e_j = -k_ent * sum_i p_ij * ln(p_ij),   0*ln0 := 0,   p clipped >= 1e-15
w_j proportional to (1 - e_j)
```
`True / False / 20 / 200 / 0 / False / equal`. All `d_j = 0` → equal.
`min_n` raised from 3: `n = 2` makes the entropy degenerate, and `ln(n) = 0` at `n = 1` divides by
zero. Applying this to raw values with mixed units or negatives gives negative `p_ij` and a
meaningless entropy — the **unit panel view is part of the contract**, not an implementation detail.
`COLL:` not handled; duplicates get identical weights and the concept is double-counted.
**Removed from the shipped default ensemble** (§4.0).

#### `critic`
CRITIC (Diakoulaki 1995): dispersion × conflict.
```
sigma_j = std(U_.j, ddof=1)
R = Pearson correlation of U after imputation (NOT listwise deletion)
C_j = sigma_j * sum_l (1 - r_jl)        # the l = j term contributes 0
w_j = C_j / sum_m C_m
```
`True / False / max(30, 3k) / 300 / 0 / False / stddev → equal`.

`C_j >= 0` always: `Σ_l (1 − r_jl) = k − Σ_l r_jl` and each `r_jl ≤ 1`, with equality only if the
column correlates perfectly with every column including itself — so `C_j = 0` exactly in that
degenerate case and positive otherwise. No clipping needed.

Two corrections. (a) "computed after imputation (listwise, per nan_policy)" is self-contradictory in
five words — imputation fills, listwise deletion drops. Impute per §2.1; with `k = 12` at 90%
per-column coverage, listwise retains `0.9^12 = 28%` of rows, and would estimate a 12×12 correlation
from a heavily selected subsample of large well-covered issuers while passing a `min_n = 5` check.
(b) **Direction guard:** `C_j` is maximised at `r = −1`, so on a panel already direction-adjusted to
higher-is-better, a metric strongly *negatively* correlated with all others — most likely mis-signed
or broken — receives the **largest** weight in the pillar. If `mean_{l≠j}(r_jl) < −0.15`, emit
`possible_mis_signed:{col}` and cap that column's `C_j` at the median. `conflict='abs'`
(`Σ_l (1 − |r_jl|)`) is exposed as the sign-error-invariant alternative. The `ρ = −1` case is a
**data-quality alarm, not a feature**.
`COLL:` **handled by construction** — near-duplicates suppress each other. This is CRITIC's whole
point and why it is in the default level-1 ensemble.

#### `stddev`
`w_j ∝ std(raw_panel_.j, ddof=1)`.
`True / False / 20 / 200 / 0 / False / equal`. All σ = 0 → equal.
Numerically the safest panel method; the backup for `critic` and the risk family.

#### `coefficient_of_variation`
`CV_j = std(X'_.j) / |mean(X'_.j)|` on the **winsorized 0..100 score panel** (not the unit panel:
its mean can sit near 0 and CV explodes; a score panel's mean is ~50). `|mean| < 1.0` → use
denominator 1.0 and warn `cv_unstable_mean:{col}`.
`True / False / 20 / 200 / 0 / False / stddev`.
Score panels are interval-scaled, so CV here is a heuristic, not a principled statistic. Documented
as such; in no default ensemble.

#### `inverse_variance`
`s2_j = var(raw_panel_.j, ddof=1)`, floored at `max(1e-8, 1e-4 · median_l s2_l)`;
`w_j ∝ 1/s2_j`.
`True / False / 20 / 200 / 0 / False / stddev → equal`.
Computing this on the `z_panel` gives every column variance 1 and silently returns equal weights — a
bug that passes every test checking only "sums to 1". A unit test asserts
`inverse_variance(z_panel) != inverse_variance(raw_panel)` on a heteroskedastic fixture.
**Anti-discriminative warning applies** (§6.3 note).

#### `inverse_volatility` *(new)*
`w_j ∝ 1 / sqrt(Sigma_jj)`.
`True / False / 20 / 200 / 0 / False / stddev → equal`.
Added so `risk_parity`'s fallback is **continuous with its own documented diagonal limit** — falling
back from risk parity to inverse *variance* is off by a square root.

---

### 6.3 Factor and risk families

> **Anti-discriminative warning — applied uniformly.** On a *score* panel, cross-sectional variance
> is **signal**, not risk: the asset-return analogy does not carry over. `min_variance`,
> `inverse_variance`, `risk_parity` and HRP's within-cluster `diag(Σ)^-1` step all reward columns
> that fail to separate stocks. The source spec warned only on `min_variance`. All four now emit
> `anti_discriminative:{method}`, and the terminal fallback of the risk family is **`stddev → equal`,
> not `inverse_variance`**, so the anti-discriminative behaviour is not what you land on whenever the
> covariance machinery fails. Concretely: a `double_sigmoid`-scored band metric saturates near 100
> across the ideal band, giving a near-constant column that `inverse_variance` loads straight to the
> `wmax = 0.40` cap — the median-relative floor caps the ratio at 10 000:1, which does not help
> because 0.40 is reached long before.

#### `pca_loadings`
```
R = corr(z_panel) + 1e-10*I ; eigh (ascending) ; reverse to lambda_1 >= ... >= lambda_k
SIGN CONVENTION: i* = argmax_i |v_mi| (lowest index on ties); if v_m,i* < 0 then v_m := -v_m
SPECTRAL-GAP GUARD: if (lambda_1 - lambda_2) < 1e-8 * lambda_1 -> fall back, warn pca_degenerate_spectrum
w_j = |v_1j| / sum_l |v_1l|
```
`True / False / max(30, 3k) / 300 / 0 / False / stddev → equal`.

The sign convention alone is **not** sufficient for determinism. Duplicate or near-duplicate columns
produce repeated eigenvalues, and `numpy.linalg.eigh` returns an arbitrary orthonormal basis of the
degenerate eigenspace — a basis that differs across LAPACK/OpenBLAS builds and thread counts. Hence
the spectral-gap guard.
PC1 is the max-variance direction, not the "quality" direction; if the fraction of positive loadings
in `v_1` lies in `(0.2, 0.8)` on an all-higher-is-better panel, warn `pc1_is_contrast`.
Correlation-PCA, not covariance-PCA, for scale invariance.
`COLL:` **not handled** — duplicates load identically on PC1 and each receives full weight. PCA
loadings are not a decorrelator despite the intuition; use `mahalanobis` or `hrp`.

#### `pca_loadings_multi`
```
clip eigenvalues at 0 first (trailing values are small negatives from roundoff when n-1 < k)
M = min( smallest m with cumulative variance share >= var_target (0.60), min(3, k-1) )
w_j proportional to sum_{m=1..M} [ lambda_m / sum_{p<=M} lambda_p ] * |v_mj|
```
`True / False / max(30, 3k) / 300 / 0 / False / pca_loadings → stddev`.

**`M` is capped at 3, and `var_target` lowered 0.80 → 0.60.** For a correlation matrix,
`Σ_m λ_m v_mj² = R_jj = 1` for every `j`, so an eigenvalue-weighted sum of squared loadings over all
components is **exactly equal weights**; the absolute-loading variant approaches that identity
monotonically as `M` grows. Advertised as "less prone to the PC1-is-a-contrast failure", its real
failure mode is being asymptotically uninformative while still summing to 1 and looking plausible. A
test asserts the weights are **not** within 1e-3 of uniform on a genuine one-factor fixture; if they
are, warn `pca_multi_uninformative` and fall back.

#### `mahalanobis`  *(alias: `decorrelated`)*
Unconstrained global minimum-variance: `w ∝ Σ^{-1} 1`.
```
Sigma = Ledoit-Wolf shrunk covariance of the UNIT panel      # view unified across the risk family
c, low = cho_factor(Sigma) ; u = cho_solve((c, low), ones(k)) ; w = u / sum(u)
```
`True / False / max(30, 3k) / 300 / 0 / True / inverse_volatility → stddev → equal`.

**View corrected from `z_panel` to `unit_panel`.** The source spec used the z-panel here and the unit
panel for `min_variance`, then documented as an identity that "`mahalanobis` IS unconstrained
min-variance". With different Σ's the claim is false and the consistency test fails even when the
unconstrained solution is entirely non-negative; anyone writing that test would "fix" the wrong
method. One view for the whole risk family — `unit` — makes the identity true and the family
internally comparable.

The `sum(u) < 0` guard is retained as a numerical backstop but **relabelled**: with LW shrinkage Σ is
PD, so `1'Σ⁻¹1 > 0` strictly and the sign flip is mathematically impossible. The real failure it was
documenting (all-constant columns → Σ = 0 → uncaught `LinAlgError`) is now handled in §5.2.
`COLL:` handled — this is the method's purpose.

#### `risk_parity`
Equal risk contribution, Spinu convex formulation, cyclical coordinate descent.
```
Sigma = LW covariance of the unit panel ; drop any column with Sigma_jj = 0 (log barrier undefined)
f(y) = 0.5 y'Sigma y - (1/k) sum_j ln(y_j)          # strictly convex on y > 0, unique minimizer
init  y_j = 1 / (k * sqrt(Sigma_jj))
sweep j = 1..k in canonical order:
    b_j = sum_{l != j} Sigma_jl y_l
    y_j = ( -b_j + sqrt(b_j^2 + 4*Sigma_jj/k) ) / (2*Sigma_jj)      # positive root
stop when max_j |dy_j| / max(y_j, 1e-12) < 1e-10, or 10000 sweeps
w = y / sum(y)
VERIFY: (max_j RC_j - min_j RC_j) <= 1e-8 * sum_j RC_j,  RC_j = w_j (Sigma w)_j
        else fall back, warn erc_not_converged
```
`True / False / max(30, 3k) / 300 / 0 / False / inverse_volatility → stddev → equal`.
**Sanity test:** with diagonal Σ the solution must equal inverse **volatility**, `w_j ∝
1/sqrt(Σ_jj)` — not inverse variance. Confusing the two is the most common risk-parity bug, and it is
why the fallback is `inverse_volatility`.
`COLL:` partially handled (correlated columns share a risk budget), less cleanly than HRP.

#### `min_variance`
Long-only minimum variance with box bounds.
```
minimize w'Sigma w  s.t.  sum w = 1,  wmin <= w_j <= wmax
primary: scipy.optimize.minimize(fun, jac=2*Sigma@w, x0=ones(k)/k, method='SLSQP',
         bounds=[(wmin,wmax)]*k, constraints=[eq sum-1 with jac=ones], ftol=1e-12, maxiter=500)
accept iff result.success and |sum(w)-1| <= 1e-6 and min(w) >= wmin-1e-9 and max(w) <= wmax+1e-9
backup:  eta = 1 / (2*lambda_max(Sigma)) ; repeat 5000x:
         w := project_box_simplex(w - eta*2*Sigma@w, wmin, wmax)      # §5.3 — NOT plain Duchi
```
`True / False / max(30, 3k) / 300 / 0 / False / risk_parity → inverse_volatility → stddev → equal`.
Defaulted **off**, excluded from every default ensemble, emits `anti_discriminative:min_variance`.
`COLL:` with exact duplicates the unconstrained problem has infinitely many optima; the shrinkage
floor (§5.2) is what restores a reproducible unique one.

#### `max_diversification`
Choueifaty–Coignard most-diversified portfolio, via its exact convex reformulation.
```
sigma_j = sqrt(Sigma_jj) floored at 1e-8 ; drop columns with Sigma_jj < 1e-16
R = Corr_hat = D^-1 Sigma D^-1
y* = argmin_y y'R y  s.t.  sum y = 1, y >= 0        # PLAIN simplex: wmin=0, wmax=1
w_j proportional to y*_j / sigma_j ; renormalize
caps applied afterwards by the post-pipeline water-filling on w
```
`True / False / max(30, 3k) / 300 / 0 / False / hrp → inverse_volatility → stddev → equal`.

**The inner solve is on the unbounded simplex.** The source spec said to reuse `min_variance`'s
bounded stack, which applies `[wmin, wmax]` to **y**, not to **w** — and since `w_j ∝ y_j/σ_j` is a
non-uniform rescaling, a `y_j` sitting exactly at 0.40 maps to a `w_j` that can land anywhere. The
result would satisfy no constraint anyone asked for. The reformulation is a standard exact
equivalence (with `y_j = w_jσ_j/(w'σ)`, `w'Σw = (w'σ)² y'Ry`, so `DR = 1/sqrt(y'Ry)`) and avoids a
non-convex ratio objective with multiple local optima; optimizing the ratio directly is the
naive-and-wrong path. Emits `max_div_caps_binding` when a cap binds, since the capped result is no
longer the exact MDP.
`COLL:` handled well — duplicates are maximally undiversifying and get suppressed.

#### `hrp` — Hierarchical Risk Parity (the recommended panel-based default)
```
STEP 1-4 use the RAW (unshrunk) Pearson correlation of the unit panel, clipped to [-1,1]:
  d_jl  = sqrt(0.5 * (1 - rho_jl))                       # correlation distance, in [0,1]
  Dt_jl = || d_.j - d_.l ||_2                            # distance of distances
  Z     = linkage(squareform(Dt, checks=False), method='ward')
  order = leaves_list(Z)                                 # quasi-diagonalization
STEP 5 uses the LW-SHRUNK Sigma for diag(Sigma_CC) and V(C):
  queue = [list(order)]
  while queue:
      L = queue.pop(0) ; if len(L) < 2: continue
      L1, L2 = L[:len(L)//2], L[len(L)//2:]
      for C in (L1, L2):
          w_inv(C) = diag(Sigma_CC)^-1 / sum(diag(Sigma_CC)^-1)
          V(C)     = w_inv(C)' Sigma_CC w_inv(C)
      den = V(L1) + V(L2)
      alpha = 0.5 if not isfinite(den) or den <= 1e-18 else 1.0 - V(L1)/den
      alpha = min(max(alpha, 0.05), 0.95)
      w[L1] *= alpha ; w[L2] *= (1 - alpha) ; queue += [L1, L2]
```
`True / False / max(30, 3k+2) / 300 / 0 / False / risk_parity → inverse_volatility → stddev → equal`.

Three corrections. (a) **`np.clip(nan, 0.05, 0.95)` returns `nan`**, so the source spec's stated
guard against `0/0` did not guard at all — the `nan` propagated through `w[L1] *= alpha` and turned
the entire weight vector into `nan`. Replaced with an explicit finite/zero test. (b) Zero-variance
columns are dropped in the shared pre-pipeline (§2 step 2); `diag(Σ_CC)^-1` divides by `Σ_jj`.
(c) **Clustering uses the unshrunk correlation.** LW shrinks every off-diagonal toward zero by
`(1−δ)`; with `k = 5..15` and `n` in the low hundreds, `δ` is routinely 0.2–0.5, which flattens the
dendrogram and makes the ward clusters far less separated than the data supports. Shrinkage exists
for *invertibility*, and HRP never inverts anything — it reads only `diag(Σ)` and the correlation
ordering. This split is deliberate and documented.

**Literature disagreement, resolved:** Lopez de Prado specifies **single** linkage; Raffinot and
others prefer **ward**. Default is **ward**, with `method='single'` exposed for fidelity. At
`k = 5..15` single linkage produces chained, maximally unbalanced dendrograms that make the recursive
bisection nearly sequential and hand the first split's minority side an arbitrary share — ward gives
balanced clusters, which is the entire point of the bisection step.
`COLL:` **handled structurally** — near-duplicates land in the same cluster and split that cluster's
budget instead of each drawing a full share. The strongest argument for HRP at the metric level.
An `hrp_equal` variant (cluster by correlation, allocate equally within and between clusters) is
available for users who accept the anti-discriminative objection to the `diag(Σ)^-1` step.

---

### 6.4 Supervised family — **hard-gated on §6.0**

#### `ic_weighted`  *(alias: `ic`)*
```
per rebalance date t: IC_j(t) = Spearman(X_t[:, j], r_{t -> t+h}) over names with both non-null
                      skip the date for column j if fewer than 20 valid pairs
ICbar_j = decay-weighted mean over the T_j retained dates      # note: T is PER COLUMN
w_j proportional to max(ICbar_j, 0)
```
`True / True / 30 / 300 / 12 / False / fixed (if a prior exists) else equal`.

`allows_negative=False` **by design**: every metric declares a direction, so a persistently negative
IC means the metric is mis-signed or broken for this regime. Silently flipping it would contradict
the declared direction and make the Explain decomposition say the opposite of the metric card.
Zero-weight it and raise `negative_ic:{col}` for a human. All `ICbar <= 0` → fall back with
`all_ics_nonpositive`.
Overlap (`h > spacing`) leaves the **mean** unbiased, so this method is safe without HAC; the
**standard error** is not, which is why `ic_ir_weighted` needs it.
`COLL:` not handled — two duplicates both score a high IC and both get full weight.

#### `ic_ir_weighted`  *(alias: `ic_ir`)*
```
build IC_j(t) as above ; L per §5.4 ; se_NW,j per §5.4, using each column's OWN T_j
EMPIRICAL BAYES toward zero (grand mean fixed at 0 = no skill):
    tau2 = max(0, mean_j(ICbar_j^2) - mean_j(se_NW,j^2))
    ICbar~_j = ICbar_j * tau2 / (tau2 + se_NW,j^2)
ICIR~_j = ICbar~_j / std(IC_j(.), ddof=1)
w_j proportional to max(ICIR~_j, 0) ; post-pipeline tau_prior = 0.25
```
`True / True / 30 / 300 / 24 / False / ic_weighted → equal`.

**`tau2` corrected from `var_j(ICbar)` to `mean_j(ICbar²) − mean_j(se²)`.** With a prior mean fixed
at 0, the method-of-moments condition is `E[ICbar²] = τ² + se²`, so the spread must be measured
*about 0*, not about the sample mean of the ICbars. The two differ by `mean_j(ICbar)²·k/(k−1)`, and
since real metric ICs share a common positive level (every metric in a curated set predicts in the
same direction), the source form is systematically too small — often exactly 0. That triggers the
spec's own failure path, so a set of metrics that **all** have genuine, uniform skill gets shrunk to
zero and falls back to equal weights, while a set with one good and one broken metric passes. The
warning is renamed `no_aggregate_ic_skill` and now fires only when the ICs are indistinguishable from
noise, which is a legitimate reason to fall back.

**Corrected rationale for the HAC step.** Overlap does *not* bias `std(IC_t)` — it induces positive
autocorrelation that inflates the variance of the **mean** by roughly `m`. The correction is
therefore applied only where the precision of `ICbar` is used (the empirical-Bayes shrinkage); the IR
denominator stays the raw per-period std, retaining its Sharpe-like per-period interpretation. The
source spec's stated reason ("std understates the sampling error by √m") would lead an implementer to
divide the denominator by `√m` as well, double-counting and deflating every ICIR. **`ICIR` is not a
t-statistic and must not be compared to 1.96.** Correct annualization is
`ICIR_annual = ICIR_period · sqrt(periods_per_year / m)` — never `ICIR · sqrt(12)` on monthly-sampled
12-month-forward ICs.
Member of the default level-1 and level-2 ensembles at 15–25%.

#### `regression`
Pooled cross-sectional elastic net; weights are standardized coefficients.
```
PER DATE: winsorize each column at 2.5/97.5, z-score cross-sectionally; z-score the forward return
          cross-sectionally too (removes the market/time effect, the largest source of spurious
          pooled R^2). Stack to (N x k), (N,).
ElasticNet(alpha, l1_ratio, fit_intercept=False, positive=config.non_negative,
           max_iter=100000, tol=1e-10, selection='cyclic', random_state=seed,
           sample_weight = time decay)
TUNING: purged forward-chaining CV (§5.5); alpha in logspace(-4, 0, 9); l1_ratio in {0.1,0.5,0.9,1.0}
SELECTION CRITERION = mean out-of-fold Spearman IC of the fitted composite, per date then averaged
                      — NOT R^2, because the product ranks stocks
w_j proportional to max(beta_j, 0)      # non_negative default True; signed betas are diagnostics only
```
`True / True / 30 / 300 / 24 (and N >= 20k) / True(declared) / ic_ir_weighted → equal`.
All betas 0 → step `alpha` down the grid until at least one is non-zero, else fall back.
`selection='cyclic'`, not `'random'` — `'random'` is non-deterministic even with a seed under some
BLAS/threading configurations.
`COLL:` elastic net with `l1_ratio < 1` exhibits the grouping effect and spreads weight across
correlated duplicates; **pure LASSO (`l1_ratio = 1.0`) picks one duplicate arbitrarily and its choice
flips with tiny data perturbations** — hence default `l1_ratio = 0.5`, with 1.0 in the grid only as
an endpoint.

#### `regression_ridge`
Closed form `beta = (X'X + n_samples·alpha·I)^{-1} X'y`.
`True / True / 30 / 300 / 24 / True(declared) / regression → ic_ir_weighted → equal`.
**The `n_samples` factor is mandatory.** sklearn's ElasticNet objective is
`1/(2n)·||y − Xw||² + α·l1·||w||₁ + 0.5α(1−l1)·||w||²`, i.e. the penalty is scaled relative to a
`1/(2n)` data term, while the naive closed form corresponds to the *unscaled* objective. Reusing the
same `alpha` grid across the two would apply penalties differing by a factor of `n_samples` — with
`N = 20k·T` rows, a factor of thousands, making `regression_ridge` effectively unpenalized OLS at
every grid point while `regression` is heavily penalized. A unit test asserts agreement with
`ElasticNet(alpha=alpha, l1_ratio=1e-8)` to 1e-8 (sklearn rejects `l1_ratio=0` exactly).

#### `newey_west_tstat`  *(deprecated alias: `grunfeld`)*
Fama–MacBeth with HAC t-statistics.
```
STAGE 1: per date t, OLS of winsorized z-scored r_{i,t->t+h} on a constant + the k z-scored columns;
         collect slope vector b_t. Require >= 20 names else skip the date.
STAGE 2: per j, OLS(b_j, ones(T)).fit(cov_type='HAC',
             cov_kwds={'maxlags': L, 'use_correction': False})     # see §5.4
         t_j = params[0] / bse[0]
w_j proportional to max(t_j, 0)         # config.use_abs -> |t_j|
```
`True / True / 30 / 300 / 24 / False / ic_ir_weighted → equal`.
**Deliberate deviation from the brief**, which specified `|t|`: `|t|` rewards a metric that reliably
predicts the **wrong** direction with a large weight while the Explain narrative still describes it
as higher-is-better — an internally contradictory report. `use_abs=True` is available.
The method is registered as `newey_west_tstat` because *Grunfeld* is a classic panel **dataset**, not
a weighting method.
`COLL:` **not handled and actively dangerous** — duplicate columns inflate each other's standard
errors and can drive both t-stats toward zero, silently deleting a real signal. Run `dedup_corr`
first or prefer `regression`.

#### `mutual_information` *(new)*
`w_j ∝ max(MI(score_j, fwd_return) − MI_null_j, 0)`, MI estimated by the Kraskov k-NN estimator
(`k = 5`) per date and averaged with time decay; `MI_null_j` is the median MI over 50 within-date
permutations, subtracted to remove the estimator's positive bias.
`True / True / 30 / 300 / 24 / False / ic_weighted → equal`.
Added because every other supervised method assumes a monotone (Spearman) or linear (elastic net)
relationship, and the design even ships a `possible_non_monotonic:{metric}` detector — with no method
able to act on the detection. A metric with genuine non-monotone predictive content gets a non-zero
weight here instead of the zero `ic_weighted`'s clip assigns it.

---

### 6.5 Attribution family

#### `shapley`
Shapley value of each column's marginal contribution to out-of-sample ranking performance.
```
v(S) = decay-weighted mean Spearman IC between the EQUALLY-WEIGHTED composite of S and the forward
       return, averaged over dates.  v(empty) := 0.
       NO inner CV: the equal-weighted composite has no fitted parameters, so out-of-fold and
       in-sample are identical and the CV is pure cost with zero leakage protection.
EXACT (k <= 12, <= 4096 subsets):
       phi_j = sum_{S subset N\{j}} [ |S|! (k-|S|-1)! / k! ] ( v(S u {j}) - v(S) )
       memoize v on frozenset(S)
MONTE CARLO (k > 12): P/2 = 1000 permutations from ctx.rng, each paired with its reverse (antithetic)
       psi_p,j = 0.5 * (phi from permutation p + phi from its reverse)
       phi_j   = mean_p(psi_p,j)
       se(phi_j) = std_p(psi_p,j, ddof=1) / sqrt(1000)
w_j proportional to max(phi_j, 0)
```
`True / True / 30 / 300 / 24 / False / ic_weighted → equal`.

**Standard error corrected.** The source spec computed `std over 2000 permutations / sqrt(2000)`, but
antithetic pairs are deliberately negatively correlated, so those 2000 draws are not independent and
that is not a valid standard error — nor is it the estimator whose precision matters. Averaging
within pairs first gives a valid i.i.d. CLT over 1000 independent pairs and correctly credits the
variance reduction. The efficiency-test tolerance `3·Σ_j se(φ_j)` and the `shapley_mc_noisy` gate
both inherit the fix. (Also: `1/sqrt(2000) = 2.24%` of the **sd**, not of the spread — those differ
by ~4× for a normal.)

Efficiency assertion (exact variant): `Σ_j φ_j == v(N)` to 1e-9.
Cache on disk under `.cache/weights/`, keyed by
`sha256(WEIGHTING_SPEC_VERSION, level, group, columns, asof, horizon, universe_hash, panel_content_hash, config_subtree)`.
**`panel_content_hash` and `WEIGHTING_SPEC_VERSION` are mandatory** — without them a bug fix in
`ic_series` or a corrected fundamentals value produces a cache hit on stale weights with no way to
detect it. Writes are atomic (temp file in the same directory, then `os.replace`).
`COLL:` **handled correctly, and this is the method's main virtue** — the Shapley value splits a
duplicated concept's credit between the duplicates by construction, which is exactly what naive IC
weighting gets wrong.
Open decision, flagged: `v(S)` uses the equal-weighted composite rather than an optimally-weighted
one. The latter is more principled but multiplies cost by the tuning loop and reintroduces
overfitting inside every one of 4096 subsets. Equal-weight ships; `eval/` settles it empirically.

#### `gradient_boosting_importance`
```
FOR EACH of the 5 purged folds:
    refit HistGradientBoostingRegressor(loss='squared_error', max_iter=300, learning_rate=0.05,
          max_depth=3, min_samples_leaf=50, l2_regularization=1.0, early_stopping=False,
          random_state=seed) on THAT FOLD'S TRAINING DATES ONLY
    permutation importance on that fold's VALIDATION dates:
        for each of n_repeats=20 and each column j: shuffle column j WITHIN each date block
        score = mean over validation dates of Spearman(prediction, realized return)
        imp contribution = base_score - permuted_score
imp_j = mean over folds and repeats ; w_j proportional to max(imp_j, 0) ; tau_prior = 0.40
```
`True / True / 30 / 300 / 36 (and N >= 100k) / False / regression → ic_ir_weighted → equal`.

**Fold scoping made explicit.** The source spec fit the model on *all* data and then computed
importances on "held-out" folds — under which every fold is in-sample for that model and the
importances are precisely the in-sample importances its own WRONG-IF-NAIVE note warns against. The
model fit on all dates through `asof` is used only for prediction, never for importances.
Use permutation importance on held-out data, **not** the built-in impurity/gain importance, which is
in-sample and biased toward high-cardinality continuous features. Shuffle **within date block** —
`sklearn.inspection.permutation_importance` shuffles globally, destroying the cross-sectional
structure and inflating every importance uniformly, so the loop is hand-written.
Determinism: the CLI entry point pins `OMP_NUM_THREADS=1` before importing sklearn, because HistGBR
binning ties resolve differently with different thread counts.
Interpretation caveat (and the reason for `τ = 0.40`): tree importance measures nonlinear and
interaction contribution, but the grader combines its inputs **linearly**, so high importance does
not imply a high optimal linear weight.
`COLL:` **splits credit poorly and silently** — permuting one duplicate leaves the model able to
recover the signal from the other, so **both** look unimportant. Run `dedup_corr` first.
Cost guard: 5 fits × 20 repeats × k scorings; `max_seconds` applies.

---

### 6.6 Meta family — all `is_composite=True`

#### `ensemble` — the recommended production default at both levels
```
members: [{method: name, weight: m_a, dedup: bool}, ...] ; normalize m to sum 1
each member: full resolve + fit + post_pipeline(apply_shrink=False)
W = sum_a m_a * w^(a)
outer: post_pipeline(W, apply_shrink=True, tau = sum_a m_a * tau_a)
a member that fails even after reaching 'equal' (only possible at k == 0) is dropped and m
renormalized over survivors, warning ensemble_member_dropped:{name}
```
`False / False / 1 / 1 / 0 / False / robust_consensus → equal` — declared preconditions are
**deliberately trivial** (§4).
Per-member `dedup: true` is the YAML syntax that satisfies the repeated "run `dedup_corr` before this
method" instruction; it is applied inside `run_members`.

**Shipped defaults (`config/weighting.yaml`):**
- level 1 (metric → pillar): `equal 0.30, critic 0.30, hrp 0.25, ic_ir_weighted 0.15`
- level 2 (pillar → final): `fixed(profile) 0.50, hrp 0.25, ic_ir_weighted 0.25`
- level 3 (profile → final): `fixed 0.70, robust_consensus 0.30`

`entropy_weight` was removed from the level-1 default and its 0.20 reallocated to `critic` (+0.10) and
`hrp` (+0.10), per §4.0. The large a-priori anchor is deliberate: it keeps grades interpretable and
stable across rebalances and bounds how far any single overfit supervised member can move a letter.

#### `robust_consensus`  *(alias: `consensus`)*
```
run members as in `ensemble` -> W_raw (A methods x k)
W_j = median_a(W_raw[a, j]) ; w = W / sum_j W_j        # renormalization is MANDATORY: the
                                                       # coordinatewise median of simplex points is
                                                       # generally NOT on the simplex
optional use_geometric_median: epsilon-smoothed Weiszfeld, distances floored at 1e-12,
    200 iterations, tol 1e-12 (the final simplex projection is a no-op for numerical cleanup only:
    the geometric median of points in a convex set already lies in that set)
ALWAYS EMIT: sigma_w,j = 1.4826 * median_a |W_raw[a,j] - W_j|
```
`False / False / 1 / 1 / 0 / False / **equal**` — **not `ensemble`**; the source spec's
`ensemble ↔ robust_consensus` pair is a two-node cycle that makes `validate_registry()` raise at
import (§4).
Requires `A >= 3` members else falls back.
The Vardi–Zhang citation is dropped: flooring the distance is epsilon-smoothing, not the VZ
correction (which involves the multiplicity `η` at a coincident point and the residual gradient).
Either is acceptable; naming one and implementing the other is not.
**Robustness property:** a single blown-up member (a supervised method that dumped 90% of the weight
on one metric) cannot move the median while a majority behave — which is why `robust_consensus`, not
`ensemble`, is the fallback for `bayesian_model_average`.

#### `bagged` *(new)*
```
for b in 1..B (default 200): resample the panel ROWS with replacement using ctx.rng
                             fit the inner method on the resample (post_pipeline, apply_shrink=False)
W   = mean_b(w^(b))
sigma_w,j = std_b(w^(b)_j, ddof=1)
```
`False / False / 1 / 1 / 0 / False / robust_consensus → equal`.
Added because `robust_consensus` measures dispersion **across methods**, which collapses to exactly
zero in the low-information cases where all members fall back to `equal` — the very cases the
confidence interval most needs to widen for. Dispersion across **bootstrap resamples of the panel**
is the honest complement, is trivially available for any panel method, and additionally stabilizes
the high-variance estimators (`pca_loadings`, `min_variance`, `regression`) far better than
shrink-to-prior does.

#### `bayesian_model_average`
```
bma_asof = asof.to_period('M').start_time              # cache key AND data cutoff, one function
for each candidate a: walk forward over T_eval dates; at each t fit w^(a) using ONLY data through
    t - 1 (expanding window, purged by e = ceil(h/spacing)); form the composite at t;
    IC_a(t) = Spearman(composite, r_{t->t+h})
ICIR_a = mean(IC_a) / se-corrected std, same NW lag as ic_ir_weighted
m_a = softmax(ICIR_a / T_temp), T_temp = 0.5 ; floor m_a at 0.02 ; renormalize
W = sum_a m_a * w^(a)   (each w^(a) refit on all data through asof) ; post_pipeline
assert max(date used) < bma_asof
```
`False / False / 1 / 1 / 36 / False / ensemble (equal member weights) → robust_consensus → equal`.

**Cache key corrected.** Truncating `asof` "to month" is a look-ahead hazard: the first grade in a
month run on the 28th would use data through the 28th and then be reused for a grade dated the 3rd —
three and a half weeks of future information, which never fails a test and inflates every backtest.
`bma_asof` is now `month_start` and is used as **both** the cache key and the data cutoff, so the two
cannot diverge, with a hard assertion.

**Honest naming caveat, stated in the docstring:** this is **not** a Bayesian model average. There is
no likelihood over weighting methods, so there are no posterior model probabilities. The tempered
softmax over out-of-sample ICIR is monotone in performance, strictly positive, bounded, and offers
one interpretable concentration knob; the name is kept because the brief asked for it. The 0.02 floor
stops a method being permanently extinguished by one bad evaluation window.

#### `entropy_pooling` — STRETCH, behind `weighting.enable_entropy_pooling`
```
prior p_i = 1/n ; views A_eq q = b (equalities), F q <= g (inequalities)
EXCLUDE the normalization row 1'q = 1 from A_eq -- it is enforced by the partition function Z.
Including it creates an exact flat direction in the dual (adding c to that multiplier multiplies
every exponent by exp(-c), which Z divides out), so the Hessian is singular, L-BFGS-B wanders along
the null direction, and convergence at ftol=1e-14 is meaningless.
rank-check A_eq and drop dependent rows.
DUAL:  L(l_eq, l_ineq) = log( sum_i p_i exp( -(A_eq' l_eq + F' l_ineq)_i ) ) + b'l_eq + g'l_ineq
       l_eq free ; l_ineq >= 0
GRAD:  dL/dl = -E_q[row] + rhs                      # supplied analytically; sign cannot be guessed
q_i = p_i exp(-(A'l*)_i) / Z
moments: mu = sum q_i x_i ; Sigma = sum q_i (x_i-mu)(x_i-mu)' / (1 - sum q_i^2)
ESS = 1 / sum q_i^2 ; REJECT the views if ESS < 0.30*n, warn views_too_strong
```
`True / False / 50 / 500 / 0 / inherits inner / hrp`.
Implement last; ship the registry without it if time is short. The ESS gate is mandatory, not
optional: strong views collapse the posterior onto a handful of rows and every downstream covariance
becomes a rank-1 fantasy.

---

## 7. Preprocessing

### 7.1 `dedup_corr`
```
clusters = complete-linkage agglomeration on squareform(1 - |rho|), cut at 1 - threshold (0.98)
run the chosen method on ONE representative per cluster (highest ctx.coverage; ties by canonical
    column order)
w_j = w_rep(cluster(j)) / |cluster(j)|
emit dedup_cluster:{members} with the MIN pairwise |rho| inside each cluster, so a human can audit
```
**Complete linkage, not single.** Single linkage transitively merges `A~B (0.98)` with `B~C (0.98)`
even when `corr(A, C) = 0.90` — the exact chaining pathology the same source spec cites as its reason
to reject single linkage for HRP. A chain of five moderately-related metrics would collapse into one
cluster and each member would be silently 5× underweighted. Complete linkage guarantees every pair
within a cluster exceeds the threshold, which is the property "these are the same concept" actually
requires.
Off by default at level 1 (metrics are curated and near-duplicates are often intentional; the
`redundancy_group` field in `METRICS.md` §4 is the curated statement of intent). **On** by default at
level 2 — pillars should be conceptually distinct, and a 0.98 pillar correlation is a design smell,
logged as `pillars_collinear`. No-op when `ctx.panel is None`.

### 7.2 Always-on redundancy diagnostic
`n_eff = (Σλ_i)² / Σλ_i²` over the pillar's score correlation matrix (participation ratio); warn
`pillar_redundant` when `n_eff < 0.5 · n_metrics`. It costs nothing and it is the single most useful
redundancy alarm in the system — three 0.95-correlated profitability metrics under the arithmetic
level-1 default are one metric with triple weight, which is the exact mechanism that poisons every
correlation-based weighting method downstream. Reported in `PillarScore.diagnostics`.

---

## 8. Config surface (`config/weighting.yaml`)

```yaml
weighting:
  seed: 20260724
  defaults: {metric_level: ensemble, pillar_level: ensemble, profile_level: ensemble}
  caps:
    metric: {wmin: 0.0, wmax: 0.40}
    pillar: {wmin: 0.0, wmax: 0.50}
    exempt_methods: [fixed, ahp]          # level 2 only
  shrinkage: {apriori_tau: 0.00, supervised_tau: 0.25, gbm_tau: 0.40}
  turnover: {enabled_supervised: true, phi: 0.50}
  min_panel_coverage: 0.60
  allow_negative_weights: false            # not user-overridable; see WEIGHTING.md §0.4
  dedup: {enabled_level1: false, enabled_level2: true, threshold: 0.98, linkage: complete}
  supervised:
    horizon_days: 63                       # READ FROM FIXTURES, not assumed
    rebalance_spacing_days: 21
    min_dates: 24
    min_names: 20
    decay_half_life_years: 3.0
    permutation_null_draws: 200
    permutation_null_max_p: 0.10
  shapley: {exact_max_k: 12, permutation_pairs: 1000}
  bagged: {draws: 200, inner: hrp}
  bma: {candidates: [...], T_temp: 0.5, floor: 0.02}
  reference_panel: {path: data/panels/reference_YYYYMMDD.csv, max_age_days: 180}
  enable_entropy_pooling: false
  max_seconds_per_method: 30
  ahp:  {<profile>: {<level>: {<group>: {matrix: {col: {col: value}}}}}}
  ensembles: {<name>: {members: [{method:, weight:, dedup:}]}}
```

---

## 9. Tests (`tests/test_weighting_contracts.py`, `tests/test_weighting_math.py`)

Contract tests, parametrized over the whole registry: `test_sums_to_one_exactly`,
`test_index_equals_ctx_columns`, `test_nonnegative_always`, `test_deterministic_across_two_runs`,
**`test_deterministic_across_two_subprocesses`** (with `PYTHONHASHSEED=random`),
`test_respects_caps_and_floors`, `test_single_stock_never_raises` (including `panel=None` **and** an
empty coverage series), `test_fallback_chains_terminate`, `test_registry_has_no_cycles_including_members`,
`test_supervised_refuse_when_not_pit`, `test_composite_runs_members_when_not_pit`,
**`test_weights_are_target_independent_up_to_renormalization`**,
**`test_weights_invariant_to_column_permutation`** (with a documented exception list — this is the
test that finds the real order-dependence bugs in HRP bisection, scipy linkage ties, the PCA sign
convention, water-filling's residual dump and dedup representative selection).

Math tests: `test_ahp_saaty_textbook_example_cr`, `test_ahp_submatrix_recomputes_ri`,
`test_entropy_uniform_column_gets_zero_weight`, `test_critic_nonnegativity_property`,
`test_critic_flags_mis_signed_column`, `test_dispersion_family_refuses_under_rank_normalizer`,
`test_inverse_variance_differs_on_z_vs_raw_panel`, `test_pca_sign_convention_stable`,
`test_pca_degenerate_spectrum_falls_back`, `test_pca_multi_not_uniform_on_one_factor`,
`test_risk_parity_diagonal_equals_inverse_volatility`,
`test_min_variance_slsqp_matches_box_projected_gradient`,
`test_max_div_reformulation_matches_bruteforce_ratio`,
`test_hrp_constant_column_no_nan`, `test_hrp_weights_positive_and_sum_one`,
`test_water_fill_terminates_and_respects_bounds`, `test_water_fill_floor_infeasible`,
`test_ledoit_wolf_zero_matrix_raises_typed`, `test_shapley_efficiency_sums_to_v_of_N`,
`test_shapley_antithetic_se_uses_pair_means`,
`test_newey_west_se_matches_statsmodels_use_correction_false` (tolerance 1e-10),
`test_nw_lag_truncation_falls_back`, `test_purged_splits_have_no_leakage`,
`test_purged_fold_one_has_training_data`, `test_ic_ir_tau2_positive_under_uniform_skill`,
`test_regression_ridge_matches_elasticnet_parameterization`,
`test_dirichlet_degenerate_agreement_does_not_report_high_confidence`.
