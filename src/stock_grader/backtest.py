"""Leakage-aware evaluation for historical stock scores.

This module does not fetch a survivorship-free universe or licensed total-return data.  It defines
the contract those inputs must satisfy and evaluates a completed point-in-time panel without
pretending that an in-sample correlation is a backtest.

Each observation must carry:

``signal_date``
    Date on which the score could have been computed.
``return_start`` / ``return_end``
    Strictly later outcome window.  Overlapping signal and outcome dates are rejected.
``ticker`` / ``score`` / ``forward_return``
    Permanent identifiers (CIK is preferred upstream), the frozen score, and a decimal total
    return that includes distributions and delisting proceeds where applicable.

If ``filed_through`` is supplied, it must be no later than ``signal_date``.  This gives tests and
data pipelines a hard invariant proving that later filings did not enter the feature set.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any, cast

import numpy as np
import pandas as pd

__all__ = [
    "BacktestConfig",
    "BacktestReport",
    "PeriodResult",
    "WalkForwardSplit",
    "backtest_to_markdown",
    "evaluate_walk_forward",
    "purged_walk_forward_splits",
]


#: Per-row round-trip cost, in basis points, written by the v6 return join
#: (:mod:`stock_grader.signal_panel`). A panel that carries it is charged name
#: by name; a panel that does not is charged ``transaction_cost_bps`` flat,
#: exactly as every panel was before this column existed.
COST_COLUMN = "round_trip_cost_bps"

#: The position the evaluator asked for, and the position the 1%-of-ADV
#: participation cap actually allowed, both written by the v6 return join.
#: Their ratio is the fraction of the intended exposure the name could be held
#: at, and it is the whole of the capacity constraint this programme exists to
#: measure. Pricing the capped slice and then holding the name at full weight
#: prices the constraint away instead of applying it: the row is charged what
#: $20k of a $2M-ADV name costs while contributing the return of $100k of it.
CAPACITY_TARGET_COLUMN = "cost_notional_target_usd"
CAPACITY_ALLOWED_COLUMN = "cost_notional_allowed_usd"


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    quantiles: int = 5
    min_cross_section: int = 20
    periods_per_year: int = 12
    transaction_cost_bps: float = 10.0
    bootstrap_samples: int = 1_000
    bootstrap_block_periods: int = 3
    seed: int = 0
    #: Set to a column name to charge per-row costs when the panel carries it.
    #: Set to ``None`` to force the flat charge even on a panel that does — the
    #: A/B that shows how much of a result the cost model is responsible for,
    #: which is a question worth being able to ask directly.
    cost_column: str | None = COST_COLUMN
    #: Weight every name by the notional the participation cap allowed it to
    #: hold, rather than equally. A name the cap could only fill 21% of holds
    #: 21% of an equal-weight position and contributes 21% of the exposure —
    #: which is what a capacity constraint IS. Set ``False`` only to reproduce
    #: an unconstrained number deliberately; the report then carries a
    #: limitation saying the constraint was priced and not applied, because
    #: that combination is the defect this flag exists to make visible.
    capacity_weighted: bool = True

    def __post_init__(self) -> None:
        if self.quantiles < 2:
            raise ValueError("quantiles must be at least 2")
        if self.min_cross_section < self.quantiles * 2:
            raise ValueError("min_cross_section must be at least twice quantiles")
        if self.periods_per_year < 1:
            raise ValueError("periods_per_year must be positive")
        if self.transaction_cost_bps < 0:
            raise ValueError("transaction_cost_bps cannot be negative")
        if self.bootstrap_samples < 0:
            raise ValueError("bootstrap_samples cannot be negative")
        if self.bootstrap_block_periods < 1:
            raise ValueError("bootstrap_block_periods must be positive")


@dataclass(slots=True)
class PeriodResult:
    signal_date: str
    return_start: str
    return_end: str
    n_securities: int
    rank_ic: float
    top_return: float
    bottom_return: float
    gross_spread: float
    net_spread: float
    top_turnover: float
    bottom_turnover: float
    quantile_returns: list[float]
    #: Equal-weight mean round-trip cost, in bps, of the names actually held in
    #: each leg this period. Under the flat charge both equal
    #: ``transaction_cost_bps``, so the field reads the same for an old panel as
    #: the constant it was always charged.
    top_cost_bps: float = 0.0
    bottom_cost_bps: float = 0.0
    #: Spearman IC of score against the SIDE-AWARE cost-net forward return:
    #: ``r - c`` for a name in the long half, ``r + c`` for one in the short
    #: half, ``r`` for a middle bucket nothing is held in. ``None`` under the
    #: flat charge, and deliberately so: subtracting one constant from every
    #: name is a monotone transform, so a flat-cost "net IC" is the gross IC
    #: wearing a different label, and printing it would suggest costs had been
    #: accounted for in a rank statistic when nothing had been.
    #:
    #: The side matters and is not a refinement. Subtracting a strictly
    #: positive cost from every name is the net return of a LONG. Because
    #: expensive names cluster at low scores, a single-signed subtraction
    #: pushes the short leg's ranked returns further down and mechanically
    #: RAISES the correlation — measured on the banded panels it made the
    #: cost-net IC larger than the gross IC, so charging realistic costs read
    #: as improving the signal while the net spread on the same run went
    #: negative. Costs destroy P&L on whichever side you take; the ranking
    #: statistic has to say so.
    rank_ic_net_side_aware: float | None = None
    #: Rows dropped this period for carrying no cost estimate. A name whose
    #: liquidity cannot be measured is a name you cannot honestly claim to have
    #: traded, so it leaves the cross-section — counted, never back-filled.
    no_cost_estimate_dropped: int = 0
    #: Fraction of the intended equal-weight exposure each leg could actually
    #: hold at the panel's declared position size, equal-weight across the
    #: leg's names. 1.0 means the participation cap never bound. Reported
    #: whether or not the weighting is applied, because a leg that could only
    #: be 21% filled is a fact about the band, not about the weighting scheme.
    top_deployable_fraction: float = 1.0
    bottom_deployable_fraction: float = 1.0
    #: Names in the two traded legs whose position the cap truncated.
    capacity_truncated_names: int = 0


@dataclass(slots=True)
class BacktestReport:
    config: BacktestConfig
    input_contract: dict[str, bool]
    periods: list[PeriodResult]
    observations: int
    rejected_periods: int
    mean_rank_ic: float
    rank_ic_information_ratio: float | None
    rank_ic_positive_rate: float
    mean_gross_spread: float
    mean_net_spread: float
    spread_positive_rate: float
    annualized_net_spread: float | None
    annualized_spread_sharpe: float | None
    max_drawdown: float
    mean_turnover: float
    quantile_monotonicity: float
    rank_ic_interval: tuple[float, float] | None
    net_spread_interval: tuple[float, float] | None
    limitations: list[str] = field(default_factory=list)
    #: Whether the per-row cost column priced this run. False means the flat
    #: ``transaction_cost_bps`` charge — the historical behaviour, unchanged.
    per_row_costs_used: bool = False
    #: Mean per-row round-trip cost across both legs and all periods, in bps.
    #: The one number that says how far the panel's own costs sit from the flat
    #: assumption it would otherwise have been charged.
    mean_round_trip_cost_bps: float | None = None
    #: Mean side-aware cost-net rank IC. ``None`` under the flat charge (see
    #: :attr:`PeriodResult.rank_ic_net_side_aware`).
    mean_rank_ic_net_side_aware: float | None = None
    #: Rows refused across the run for want of a cost estimate.
    no_cost_estimate_rows: int = 0
    #: Whether leg returns and leg costs are weighted by the notional the
    #: participation cap allowed. False on a panel that carries no capacity
    #: columns, or when the caller switched it off — both cases add a
    #: limitation rather than passing silently.
    capacity_weighted: bool = False
    #: Mean fraction of the intended exposure the traded legs could hold,
    #: across both legs and all periods. ``None`` when the panel says nothing
    #: about capacity. Below 1.0 every spread in this report is per DEPLOYED
    #: dollar: the strategy could not have carried the position it was scored
    #: on, and the shortfall is the capacity constraint, not a rounding
    #: detail.
    mean_deployable_fraction: float | None = None
    #: The ADV-band block carried by the panel's own sidecar, or ``None`` for a
    #: panel that is not a band of a pre-registered partition. When it is
    #: present and says ``reportable: false``, the band sits below the
    #: program's declared evaluable-period floor and NOTHING in this report may
    #: be quoted as a band statistic — the limitation says so in words, and the
    #: caller had to pass an explicit override to get here at all.
    adv_band: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    train_dates: tuple[pd.Timestamp, ...]
    embargo_dates: tuple[pd.Timestamp, ...]
    test_dates: tuple[pd.Timestamp, ...]


def _single_universe_id(panel: pd.DataFrame) -> bool:
    """Whether an optional universe_id column describes at most one universe."""
    if "universe_id" not in panel:
        return True
    identifiers = panel["universe_id"].astype("string")
    return bool(identifiers.nunique(dropna=False) <= 1)


def _validate_panel(panel: pd.DataFrame, *, allow_mixed_universes: bool = False) -> pd.DataFrame:
    required = {
        "signal_date",
        "return_start",
        "return_end",
        "ticker",
        "score",
        "forward_return",
    }
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError("backtest panel is missing columns: " + ", ".join(missing))
    if not allow_mixed_universes and not _single_universe_id(panel):
        raise ValueError(
            "backtest panel mixes multiple universe_id values; pass "
            "allow_mixed_universes=True only for an explicitly caveated analysis"
        )
    frame = panel.copy()
    for column in ("signal_date", "return_start", "return_end"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
        if frame[column].isna().any():
            raise ValueError(f"{column} contains invalid dates")
    if "filed_through" in frame:
        frame["filed_through"] = pd.to_datetime(frame["filed_through"], errors="coerce")
        if frame["filed_through"].isna().any():
            raise ValueError("filed_through contains invalid dates")
        leaked = frame["filed_through"] > frame["signal_date"]
        if leaked.any():
            raise ValueError(
                f"{int(leaked.sum())} observations use filings after their signal_date"
            )
    if (frame["return_start"] <= frame["signal_date"]).any():
        raise ValueError("every return_start must be strictly after signal_date")
    if (frame["return_end"] <= frame["return_start"]).any():
        raise ValueError("every return_end must be strictly after return_start")
    permanent_id = _permanent_id_column(frame)
    frame["_security_key"] = (
        frame[permanent_id].astype(str) if permanent_id is not None else frame["ticker"].astype(str)
    )
    duplicate_ticker = frame.duplicated(["signal_date", "ticker"])
    duplicate_security = frame.duplicated(["signal_date", "_security_key"])
    if duplicate_ticker.any() or duplicate_security.any():
        raise ValueError(
            "duplicate signal_date/security observations are not allowed "
            "(checked both ticker and permanent identifier)"
        )
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    frame["forward_return"] = pd.to_numeric(frame["forward_return"], errors="coerce")
    finite = np.isfinite(frame["score"]) & np.isfinite(frame["forward_return"])
    frame = frame.loc[finite].copy()
    if (frame["forward_return"] < -1.0).any():
        raise ValueError("forward_return cannot be below -100%")
    return frame.sort_values(["signal_date", "_security_key", "ticker"]).reset_index(drop=True)


def _attested(panel: pd.DataFrame, column: str) -> bool:
    """Return true only when every observation explicitly attests to a data property."""

    if column not in panel or panel.empty:
        return False
    values = panel[column]
    if values.isna().any():
        return False
    truthy = {"1", "true", "yes", "y"}
    return bool(values.map(lambda item: str(item).strip().lower() in truthy).all())


def _permanent_id_column(panel: pd.DataFrame) -> str | None:
    for column in ("cik", "security_id", "permanent_id"):
        if column not in panel or panel[column].isna().any():
            continue
        if panel[column].astype(str).str.strip().ne("").all():
            return column
    return None


def _input_contract(panel: pd.DataFrame) -> dict[str, bool]:
    permanent_id = _permanent_id_column(panel)
    return {
        # .all() hands back a numpy bool_, so without bool() this dict's values are a
        # mix of Python and numpy booleans depending on which branch short-circuits.
        "filing_cutoff_provided": bool(
            "filed_through" in panel and panel["filed_through"].notna().all()
        ),
        "point_in_time_universe_attested": _attested(panel, "universe_is_pit"),
        "total_returns_attested": _attested(panel, "return_is_total"),
        "delistings_included_attested": _attested(panel, "delisting_return_included"),
        "permanent_identifier_present": permanent_id is not None,
        "single_universe_id": _single_universe_id(panel),
    }


def _turnover(previous: set[str] | None, current: set[str]) -> float:
    if not current:
        return 0.0
    # Entering the first portfolio is a real trade. Treat an absent prior portfolio as cash,
    # otherwise a short sample receives an artificially cheap first period.
    if previous is None or not previous:
        return 1.0
    overlap = len(previous & current)
    return float(1.0 - 2.0 * overlap / (len(previous) + len(current)))


def _weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    """Mean of ``values`` per unit of ``weights``, or NaN when nothing is held.

    NaN rather than 0.0 on an empty leg: a leg the capacity cap refused
    entirely earned no return, and "no return" is not "a return of zero" — the
    caller drops the period instead of averaging in a number nobody could have
    made.
    """

    total = float(weights.sum())
    if not math.isfinite(total) or total <= 0.0:
        return float("nan")
    return float((values * weights).sum() / total)


def _quantile_buckets(scores: pd.Series, quantiles: int) -> pd.Series:
    # Average ranks keep tied scores in the same percentile rather than assigning arbitrary
    # winners based on input order.  The floor produces integer buckets 0..quantiles-1.
    percentiles = scores.rank(method="average", pct=True)
    # .clip(upper=) is the Series-native spelling of the elementwise np.minimum this
    # used to call, and keeps the return a Series rather than degrading to an ndarray.
    buckets = ((percentiles * quantiles).apply(np.ceil) - 1).clip(upper=quantiles - 1)
    return buckets.astype("int64")


def _annualized_return(returns: np.ndarray, periods_per_year: int) -> float | None:
    if returns.size == 0 or np.any(returns <= -1.0):
        return None
    growth = float(np.prod(1.0 + returns))
    return growth ** (periods_per_year / returns.size) - 1.0


def _max_drawdown(returns: np.ndarray) -> float:
    if returns.size == 0:
        return float("nan")
    wealth = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(np.concatenate(([1.0], wealth)))[1:]
    drawdowns = wealth / peak - 1.0
    return float(drawdowns.min(initial=0.0))


def _moving_block_interval(
    values: np.ndarray,
    *,
    samples: int,
    block_size: int,
    seed: int,
) -> tuple[float, float] | None:
    clean = np.asarray(values, dtype="float64")
    clean = clean[np.isfinite(clean)]
    if clean.size < 2 or samples < 1:
        return None
    rng = np.random.default_rng(seed)
    size = clean.size
    block = min(block_size, size)
    starts = np.arange(size)
    means = np.empty(samples, dtype="float64")
    blocks_needed = math.ceil(size / block)
    for draw in range(samples):
        chunks = []
        for _ in range(blocks_needed):
            start = int(rng.choice(starts))
            indexes = (start + np.arange(block)) % size
            chunks.append(clean[indexes])
        means[draw] = float(np.concatenate(chunks)[:size].mean())
    low, high = np.percentile(means, [2.5, 97.5])
    return (float(low), float(high))


def evaluate_walk_forward(
    panel: pd.DataFrame,
    config: BacktestConfig | None = None,
    *,
    allow_mixed_universes: bool = False,
    adv_band: dict | None = None,
) -> BacktestReport:
    """Evaluate frozen point-in-time scores against strictly later total returns.

    ``adv_band`` is the band block from the panel's sidecar (see
    :func:`stock_grader.signal_panel.band_report`). It changes no number here —
    a band's arithmetic is the same arithmetic — but a band the pre-registered
    floor refuses must not produce a report that reads like any other, so it is
    carried onto the report and stated as the first limitation.
    """

    config = config or BacktestConfig()
    contract = _input_contract(panel)
    frame = _validate_panel(panel, allow_mixed_universes=allow_mixed_universes)
    periods: list[PeriodResult] = []
    rejected = 0
    previous_top: set[str] | None = None
    previous_bottom: set[str] | None = None
    cost_rate = config.transaction_cost_bps / 10_000.0
    # Per-row costs are used only when the panel actually carries them. Every
    # panel built before the cost column existed takes the branch below with
    # `per_row_costs` False and reaches byte-identical numbers through the same
    # arithmetic it always did — the flat path is not a fallback bolted on, it
    # is this path with one constant instead of a column.
    cost_column = config.cost_column
    per_row_costs = bool(cost_column) and cost_column in frame.columns
    refused_by_date: dict[pd.Timestamp, int] = {}
    if per_row_costs and cost_column is not None:
        values = pd.to_numeric(frame[cost_column], errors="coerce").to_numpy(dtype="float64")
        if np.any(values[np.isfinite(values)] < 0.0):
            raise ValueError(f"{cost_column} cannot be negative")
        estimable = np.isfinite(values)
        refused_by_date = {
            # Same widening as the main loop below: a datetime64 column's group keys
            # are Timestamps at runtime but typed as bare Hashable.
            pd.Timestamp(cast(Any, key)): int(count)
            for key, count in frame.loc[~estimable].groupby("signal_date", sort=True).size().items()
        }
        frame = frame.loc[estimable].copy()
        if frame.empty:
            raise ValueError(
                f"every row carries a null {cost_column}: the panel says it prices "
                "costs per row and can price none of them, so there is nothing to "
                "evaluate net. Rebuild with the cost inputs present, or evaluate it "
                "explicitly gross by passing cost_column=None."
            )
        frame["_cost_rate"] = values[estimable] / 10_000.0

    # The capacity constraint, applied rather than priced. `cost_notional_
    # allowed_usd / cost_notional_target_usd` is the fraction of the intended
    # position the 1%-of-ADV cap let through; the row is held at exactly that
    # fraction of an equal weight, so its return and its cost both describe the
    # same dollars. Without this the evaluator charges the cost of the slice
    # that fit and books the return of the whole order, which flatters the
    # thinnest band by construction — and the thinnest band is the whole point
    # of the measurement.
    capacity_weighted = False
    capacity_available = per_row_costs and {
        CAPACITY_TARGET_COLUMN,
        CAPACITY_ALLOWED_COLUMN,
    } <= set(frame.columns)
    if capacity_available:
        target = pd.to_numeric(frame[CAPACITY_TARGET_COLUMN], errors="coerce").to_numpy(
            dtype="float64"
        )
        allowed = pd.to_numeric(frame[CAPACITY_ALLOWED_COLUMN], errors="coerce").to_numpy(
            dtype="float64"
        )
        usable = np.isfinite(target) & np.isfinite(allowed) & (target > 0.0) & (allowed >= 0.0)
        if not usable.all():
            raise ValueError(
                f"{int((~usable).sum())} priced row(s) carry an unusable "
                f"{CAPACITY_TARGET_COLUMN}/{CAPACITY_ALLOWED_COLUMN} pair. A row that "
                "has a cost but no position size cannot be weighted by the exposure it "
                "could hold, and evaluating it at full weight would price the capacity "
                "constraint away. Fix the panel rather than dropping the constraint."
            )
        # Clipped, not trusted: allowed > target would be a builder defect, and
        # it must not become a leveraged position here.
        frame["_fill_fraction"] = np.clip(allowed / target, 0.0, 1.0)
        capacity_weighted = bool(config.capacity_weighted)

    for group_key, group in frame.groupby("signal_date", sort=True):
        # The stubs widen groupby keys to a bare Scalar; this column is datetime64,
        # so normalise once here rather than re-wrapping at each downstream use.
        signal_date = pd.Timestamp(cast(Any, group_key))
        if len(group) < config.min_cross_section or group["score"].nunique() < 2:
            rejected += 1
            continue
        # One signal date must correspond to one outcome window.  Mixing horizons makes a mean
        # spread uninterpretable and can overlap training/test outcomes in a later model fit.
        starts = group["return_start"].drop_duplicates()
        ends = group["return_end"].drop_duplicates()
        if len(starts) != 1 or len(ends) != 1:
            raise ValueError(f"signal_date {signal_date.date()} mixes return windows")

        rank_ic = float(group["score"].corr(group["forward_return"], method="spearman"))
        buckets = _quantile_buckets(group["score"], config.quantiles)
        if capacity_weighted:
            fills = group["_fill_fraction"]
            weight_by_bucket = fills.groupby(buckets).sum()
            bucket_returns = (fills * group["forward_return"]).groupby(
                buckets
            ).sum() / weight_by_bucket
        else:
            bucket_returns = group.groupby(buckets)["forward_return"].mean()
        if 0 not in bucket_returns or config.quantiles - 1 not in bucket_returns:
            rejected += 1
            continue
        quantile_returns = [
            float(bucket_returns.get(index, np.nan)) for index in range(config.quantiles)
        ]
        top_mask = buckets == config.quantiles - 1
        bottom_mask = buckets == 0
        top_names = set(group.loc[top_mask, "_security_key"].astype(str))
        bottom_names = set(group.loc[bottom_mask, "_security_key"].astype(str))
        if capacity_weighted:
            top_return = _weighted_mean(
                group.loc[top_mask, "forward_return"], group.loc[top_mask, "_fill_fraction"]
            )
            bottom_return = _weighted_mean(
                group.loc[bottom_mask, "forward_return"],
                group.loc[bottom_mask, "_fill_fraction"],
            )
            if not (math.isfinite(top_return) and math.isfinite(bottom_return)):
                # A leg the cap refused entirely holds nothing, and the return
                # of nothing is not zero, it is undefined. The period drops out
                # counted rather than contributing a fabricated 0%.
                rejected += 1
                continue
        else:
            top_return = float(group.loc[top_mask, "forward_return"].mean())
            bottom_return = float(group.loc[bottom_mask, "forward_return"].mean())
        # Turnover is set membership and is unaffected by weighting: the names
        # entering and leaving a leg are the same names either way.
        top_turnover = _turnover(previous_top, top_names)
        bottom_turnover = _turnover(previous_bottom, bottom_names)
        previous_top, previous_bottom = top_names, bottom_names
        gross_spread = top_return - bottom_return
        # The charge is the same shape it has always been — a rate applied to
        # the fraction of each leg that actually turned over — with one change:
        # the rate is the equal-weight mean cost of the names in THAT leg
        # instead of one number for the whole market. A leg of thin names is
        # charged what thin names cost; the flat charge is the special case
        # where every name costs the same, and it reduces to it exactly.
        if per_row_costs:
            if capacity_weighted:
                # The rate the leg actually pays per DEPLOYED dollar. Weighting
                # the return but not the cost would charge a $20k fill at the
                # rate of a $100k one.
                top_rate = _weighted_mean(
                    group.loc[top_mask, "_cost_rate"], group.loc[top_mask, "_fill_fraction"]
                )
                bottom_rate = _weighted_mean(
                    group.loc[bottom_mask, "_cost_rate"],
                    group.loc[bottom_mask, "_fill_fraction"],
                )
            else:
                top_rate = float(group.loc[top_mask, "_cost_rate"].mean())
                bottom_rate = float(group.loc[bottom_mask, "_cost_rate"].mean())
            # A cost is a loss on WHICHEVER side you take it. The long half is
            # ranked against r - c and the short half against r + c; the middle
            # buckets are held on neither side and pay nothing. Subtracting c
            # from every name instead is the net return of a long-only book,
            # and on a real panel — where the expensive names sit at the bottom
            # of the score — it pushes the short leg's returns down and RAISES
            # the correlation, so the report would say that charging honest
            # costs improved the signal.
            side = np.sign(buckets.to_numpy(dtype="float64") - (config.quantiles - 1) / 2.0)
            net_return = (
                group["forward_return"] - pd.Series(side, index=group.index) * group["_cost_rate"]
            )
            rank_ic_net: float | None = float(group["score"].corr(net_return, method="spearman"))
            net_spread = gross_spread - (top_rate * top_turnover + bottom_rate * bottom_turnover)
        else:
            top_rate = bottom_rate = cost_rate
            rank_ic_net = None
            # Deliberately the ORIGINAL expression, not the two-rate form with
            # both rates equal. Those differ in the last bit of a float, and a
            # published number that moves in its last bit is a number that has
            # moved. Panels with no cost column reproduce exactly.
            net_spread = gross_spread - cost_rate * (top_turnover + bottom_turnover)
        periods.append(
            PeriodResult(
                signal_date=signal_date.date().isoformat(),
                return_start=starts.iloc[0].date().isoformat(),
                return_end=ends.iloc[0].date().isoformat(),
                n_securities=len(group),
                rank_ic=rank_ic,
                top_return=top_return,
                bottom_return=bottom_return,
                gross_spread=gross_spread,
                net_spread=net_spread,
                top_turnover=top_turnover,
                bottom_turnover=bottom_turnover,
                quantile_returns=quantile_returns,
                top_cost_bps=top_rate * 10_000.0,
                bottom_cost_bps=bottom_rate * 10_000.0,
                rank_ic_net_side_aware=rank_ic_net,
                no_cost_estimate_dropped=int(refused_by_date.get(signal_date, 0)),
                top_deployable_fraction=(
                    float(group.loc[top_mask, "_fill_fraction"].mean())
                    if capacity_available
                    else 1.0
                ),
                bottom_deployable_fraction=(
                    float(group.loc[bottom_mask, "_fill_fraction"].mean())
                    if capacity_available
                    else 1.0
                ),
                capacity_truncated_names=(
                    int((group.loc[top_mask | bottom_mask, "_fill_fraction"] < 1.0).sum())
                    if capacity_available
                    else 0
                ),
            )
        )

    if not periods:
        raise ValueError(
            "no period met the minimum cross-section and score-dispersion requirements"
        )
    rank_ics = np.asarray([item.rank_ic for item in periods], dtype="float64")
    gross = np.asarray([item.gross_spread for item in periods], dtype="float64")
    net = np.asarray([item.net_spread for item in periods], dtype="float64")
    turnovers = np.asarray(
        [(item.top_turnover + item.bottom_turnover) / 2.0 for item in periods],
        dtype="float64",
    )
    quantile_matrix = np.asarray([item.quantile_returns for item in periods], dtype="float64")
    mean_quantiles = np.nanmean(quantile_matrix, axis=0)
    monotonicity = float(
        pd.Series(np.arange(config.quantiles)).corr(pd.Series(mean_quantiles), method="spearman")
    )
    ic_std = float(np.std(rank_ics, ddof=1)) if len(rank_ics) > 1 else 0.0
    ic_ir = (
        float(np.mean(rank_ics) / ic_std * math.sqrt(config.periods_per_year))
        if ic_std > 0
        else None
    )
    refused_rows = int(sum(refused_by_date.values()))
    deployable = (
        float(
            np.mean(
                [
                    (item.top_deployable_fraction + item.bottom_deployable_fraction) / 2.0
                    for item in periods
                ]
            )
        )
        if capacity_available
        else None
    )
    spread_std = float(np.std(net, ddof=1)) if len(net) > 1 else 0.0
    spread_sharpe = (
        float(np.mean(net) / spread_std * math.sqrt(config.periods_per_year))
        if spread_std > 0
        else None
    )
    bootstrap_note = (
        "Bootstrap intervals describe historical period variability, not future-return certainty."
    )
    if per_row_costs:
        limitations = [
            (
                "Transaction costs are per-name, per-date estimates (spread plus "
                "modelled impact at a declared position size). They still exclude "
                "borrow, commissions, exchange and regulatory fees, halts, timing "
                "risk within the session, and any adverse selection conditional on "
                "the signal."
            ),
            bootstrap_note,
        ]
        if capacity_weighted and deployable is not None and deployable < 1.0:
            limitations.append(
                f"The participation cap could fill only {deployable:.1%} of the "
                "intended per-name position in the traded legs, so returns and costs "
                "are weighted by the notional each name could actually hold. Every "
                "spread here is therefore per DEPLOYED dollar: at this position size "
                "the strategy could not have carried the book it is scored on, and "
                "that shortfall is the capacity constraint itself, not a rounding "
                "detail. Re-price the panel at a smaller notional to read a number "
                "the cap does not bind on."
            )
        elif capacity_available and not config.capacity_weighted:
            limitations.append(
                "Capacity weighting was switched off: a name the participation cap "
                "could only partly fill is charged the cost of the slice that fit "
                "while contributing the return of the whole intended position. That "
                "prices the capacity constraint away instead of applying it and "
                "flatters the thinnest names by construction. These numbers are an "
                "unconstrained counterfactual, not a tradable result."
            )
        elif not capacity_available:
            limitations.append(
                f"The panel carries per-row costs but no {CAPACITY_TARGET_COLUMN}/"
                f"{CAPACITY_ALLOWED_COLUMN} pair, so nothing here can tell whether a "
                "participation cap truncated a position. If the cost column was "
                "priced on a capped slice, every truncated name is charged the cost "
                "of the fraction that fit while held at full weight."
            )
    else:
        limitations = [
            (
                "Transaction costs are a fixed turnover charge and do not model "
                "market impact or borrow."
            ),
            bootstrap_note,
        ]
        if config.cost_column:
            limitations.append(
                "The panel carries no per-row cost column, so one flat rate priced "
                "every name. A flat rate undercharges thinly traded names and "
                "overcharges liquid ones, which biases any cross-liquidity comparison "
                "in favour of the thin side."
            )
    # First in the list, and unconditional on anything else: a band below the
    # floor is not a caveat on a result, it is the absence of one.
    if isinstance(adv_band, dict) and adv_band and not adv_band.get("reportable", False):
        because = adv_band.get("not_reportable_because") or []
        limitations.insert(
            0,
            (
                f"NOT REPORTABLE: ADV band {adv_band.get('band_id') or '?'} is below the "
                f"pre-registered evaluable-period floor of "
                f"{adv_band.get('min_evaluable_periods')} "
                f"({'; '.join(str(item) for item in because) or 'no reason recorded'}). "
                "The pre-registration permits no band statistic from this panel: every "
                "number below is an exploratory diagnostic on an under-powered sample "
                "and must not be quoted as a band result, compared against another "
                "band, or counted as a trial of the declared hypothesis."
            ),
        )
    if refused_rows:
        limitations.append(
            f"{refused_rows} row(s) carried no cost estimate and were dropped from "
            "the evaluated cross-section rather than charged a substituted cost. The "
            "names that cannot be priced are not a random sample of the panel."
        )
    if not contract["point_in_time_universe_attested"]:
        limitations.append(
            "The panel does not attest to a survivorship-free point-in-time universe."
        )
    if not contract["total_returns_attested"]:
        limitations.append("The panel does not attest that forward_return includes distributions.")
    if not contract["delistings_included_attested"]:
        limitations.append(
            "The panel does not attest that delisting proceeds or total losses are retained."
        )
    if not contract["filing_cutoff_provided"]:
        limitations.append(
            "No filed_through column proves that fundamentals were public by signal_date."
        )
    if not contract["permanent_identifier_present"]:
        limitations.append(
            "No permanent security identifier is present; ticker reuse can join the wrong issuer."
        )

    if not contract["single_universe_id"]:
        limitations.append(
            "The panel mixes universe_id values, so cross-sectional breadth changes across periods."
        )
    return BacktestReport(
        config=config,
        input_contract=contract,
        periods=periods,
        observations=sum(item.n_securities for item in periods),
        rejected_periods=rejected,
        mean_rank_ic=float(np.mean(rank_ics)),
        rank_ic_information_ratio=ic_ir,
        rank_ic_positive_rate=float(np.mean(rank_ics > 0)),
        mean_gross_spread=float(np.mean(gross)),
        mean_net_spread=float(np.mean(net)),
        spread_positive_rate=float(np.mean(net > 0)),
        annualized_net_spread=_annualized_return(net, config.periods_per_year),
        annualized_spread_sharpe=spread_sharpe,
        max_drawdown=_max_drawdown(net),
        mean_turnover=float(np.mean(turnovers)),
        quantile_monotonicity=monotonicity,
        rank_ic_interval=_moving_block_interval(
            rank_ics,
            samples=config.bootstrap_samples,
            block_size=config.bootstrap_block_periods,
            seed=config.seed,
        ),
        net_spread_interval=_moving_block_interval(
            net,
            samples=config.bootstrap_samples,
            block_size=config.bootstrap_block_periods,
            seed=config.seed + 1,
        ),
        limitations=limitations,
        per_row_costs_used=per_row_costs,
        mean_round_trip_cost_bps=(
            float(np.mean([(item.top_cost_bps + item.bottom_cost_bps) / 2.0 for item in periods]))
            if per_row_costs
            else None
        ),
        mean_rank_ic_net_side_aware=(
            float(np.mean([item.rank_ic_net_side_aware for item in periods]))
            if per_row_costs
            else None
        ),
        no_cost_estimate_rows=refused_rows,
        capacity_weighted=capacity_weighted,
        mean_deployable_fraction=deployable,
        adv_band=(dict(adv_band) if isinstance(adv_band, dict) and adv_band else None),
    )


def purged_walk_forward_splits(
    dates: pd.Series | pd.Index | list,
    *,
    train_periods: int,
    test_periods: int,
    embargo_periods: int = 1,
    step_periods: int | None = None,
) -> Iterator[WalkForwardSplit]:
    """Yield expanding-window chronological splits with an explicit embargo."""

    if train_periods < 2 or test_periods < 1 or embargo_periods < 0:
        raise ValueError("train_periods>=2, test_periods>=1, and embargo_periods>=0 are required")
    unique = tuple(sorted(pd.Timestamp(item) for item in pd.Index(dates).unique()))
    step = step_periods or test_periods
    if step < 1:
        raise ValueError("step_periods must be positive")
    train_end = train_periods
    while train_end + embargo_periods + test_periods <= len(unique):
        yield WalkForwardSplit(
            train_dates=unique[:train_end],
            embargo_dates=unique[train_end : train_end + embargo_periods],
            test_dates=unique[
                train_end + embargo_periods : train_end + embargo_periods + test_periods
            ],
        )
        train_end += step


def backtest_to_markdown(report: BacktestReport) -> str:
    """Render a compact, auditable validation summary.

    Intervals are labelled as historical resampling intervals. They are not forecasts and do not
    cure survivorship, delisting, or point-in-time defects in the caller-supplied panel.
    """

    def number(value: float | None, *, percent: bool = False) -> str:
        if value is None or not math.isfinite(value):
            return "—"
        return f"{value:.2%}" if percent else f"{value:.3f}"

    def interval(value: tuple[float, float] | None, *, percent: bool = False) -> str:
        if value is None or not all(math.isfinite(item) for item in value):
            return "—"
        if percent:
            return f"[{value[0]:.2%}, {value[1]:.2%}]"
        return f"[{value[0]:.3f}, {value[1]:.3f}]"

    def dollars(value: object) -> str:
        if not isinstance(value, (int, float)):
            return "none" if value is None else str(value)
        return f"${float(value):,.0f}"

    band = report.adv_band if isinstance(report.adv_band, dict) else None
    band_lines: list[str] = []
    if band:
        raw_edges = band.get("band")
        edges: dict = raw_edges if isinstance(raw_edges, dict) else {}
        raw_observations = band.get("observations")
        observations: dict = raw_observations if isinstance(raw_observations, dict) else {}
        verdict = "REPORTABLE" if band.get("reportable") else "NOT REPORTABLE — REFUSED"
        band_lines = [
            f"## ADV band {band.get('band_id') or '?'} — {verdict}",
            "",
            "| band property | value |",
            "|---|---|",
            f"| Band | {edges.get('band_id') or '?'} ({edges.get('label') or '—'}) |",
            (
                "| Trailing-20-session median dollar volume | "
                f"{dollars(edges.get('adv_floor'))} to "
                f"{dollars(edges.get('adv_cap')) if edges.get('adv_cap') is not None else 'no cap'}"
                " |"
            ),
            f"| Control band | {'yes' if edges.get('is_control') else 'no'} |",
            (
                "| Evaluable periods (this panel / floor) | "
                f"{band.get('evaluable_periods')} / {band.get('min_evaluable_periods')} |"
            ),
            # The upstream count too: the verdict is the AND of both artifacts,
            # so a table showing only this panel's periods would look like it
            # cleared a floor the observation panel failed.
            (
                "| Evaluable periods (observation panel) | "
                f"{observations.get('evaluable_periods') if observations else '—'} |"
            ),
            f"| Pre-registration | {band.get('preregistration') or '—'} |",
            f"| Pre-registration sha256 | {band.get('preregistration_sha256') or '—'} |",
            "",
            *(
                []
                if band.get("reportable")
                else [
                    (
                        "> The pre-registered floor refuses this band. The diagnostics "
                        "below are exploratory only and are not a band result."
                    ),
                    "",
                ]
            ),
        ]

    lines = [
        "# Walk-forward score validation",
        "",
        (
            f"{len(report.periods)} accepted periods, {report.observations} observations, "
            f"{report.rejected_periods} rejected periods."
        ),
        "",
        *band_lines,
        "## Input contract",
        "",
        "| requirement | verified by panel |",
        "|---|:---:|",
        *[
            f"| {name.replace('_', ' ')} | {'yes' if passed else 'NO'} |"
            for name, passed in report.input_contract.items()
        ],
        "",
        "## Diagnostics",
        "",
        "| diagnostic | result |",
        "|---|---:|",
        f"| Mean cross-sectional rank IC | {number(report.mean_rank_ic)} |",
        (f"| Moving-block 95% interval for mean rank IC | {interval(report.rank_ic_interval)} |"),
        f"| Rank IC information ratio | {number(report.rank_ic_information_ratio)} |",
        f"| Positive rank-IC periods | {number(report.rank_ic_positive_rate, percent=True)} |",
        f"| Mean gross top-minus-bottom spread | {number(report.mean_gross_spread, percent=True)} |",
        f"| Mean net top-minus-bottom spread | {number(report.mean_net_spread, percent=True)} |",
        (
            "| Moving-block 95% interval for mean net spread | "
            f"{interval(report.net_spread_interval, percent=True)} |"
        ),
        f"| Annualized net spread | {number(report.annualized_net_spread, percent=True)} |",
        f"| Annualized spread Sharpe | {number(report.annualized_spread_sharpe)} |",
        f"| Net-spread maximum drawdown | {number(report.max_drawdown, percent=True)} |",
        f"| Mean one-way portfolio turnover | {number(report.mean_turnover, percent=True)} |",
        f"| Mean quantile monotonicity | {number(report.quantile_monotonicity)} |",
        # Only on a panel that carries per-row costs. Printing "flat 10 bps" as
        # a diagnostic on every old report would restate the config as though
        # it were a measurement.
        *(
            [
                (
                    "| Cost model | per-row (mean round trip "
                    f"{number(report.mean_round_trip_cost_bps)} bps) |"
                ),
                (
                    "| Mean side-aware cost-net rank IC | "
                    f"{number(report.mean_rank_ic_net_side_aware)} |"
                ),
                f"| Rows without a cost estimate | {report.no_cost_estimate_rows} |",
                (f"| Capacity-weighted exposure | {'yes' if report.capacity_weighted else 'NO'} |"),
                (
                    "| Mean deployable fraction of intended position | "
                    f"{number(report.mean_deployable_fraction, percent=True)} |"
                ),
            ]
            if report.per_row_costs_used
            else []
        ),
        "",
        "## Assumptions and limitations",
        "",
        *[f"- {item}" for item in report.limitations],
        "",
        (
            "These are historical diagnostics for a frozen score panel, not a forecast, "
            "investment recommendation, or proof that the model will persist."
        ),
    ]
    return "\n".join(lines)
