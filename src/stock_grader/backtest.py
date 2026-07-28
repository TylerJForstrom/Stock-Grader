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
from dataclasses import asdict, dataclass, field
from typing import Iterator

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


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    quantiles: int = 5
    min_cross_section: int = 20
    periods_per_year: int = 12
    transaction_cost_bps: float = 10.0
    bootstrap_samples: int = 1_000
    bootstrap_block_periods: int = 3
    seed: int = 0

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

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    train_dates: tuple[pd.Timestamp, ...]
    embargo_dates: tuple[pd.Timestamp, ...]
    test_dates: tuple[pd.Timestamp, ...]


def _validate_panel(panel: pd.DataFrame) -> pd.DataFrame:
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
        frame[permanent_id].astype(str)
        if permanent_id is not None
        else frame["ticker"].astype(str)
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
        "filing_cutoff_provided": "filed_through" in panel and panel["filed_through"].notna().all(),
        "point_in_time_universe_attested": _attested(panel, "universe_is_pit"),
        "total_returns_attested": _attested(panel, "return_is_total"),
        "delistings_included_attested": _attested(panel, "delisting_return_included"),
        "permanent_identifier_present": permanent_id is not None,
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


def _quantile_buckets(scores: pd.Series, quantiles: int) -> pd.Series:
    # Average ranks keep tied scores in the same percentile rather than assigning arbitrary
    # winners based on input order.  The floor produces integer buckets 0..quantiles-1.
    percentiles = scores.rank(method="average", pct=True)
    buckets = np.minimum((percentiles * quantiles).apply(np.ceil) - 1, quantiles - 1)
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
) -> BacktestReport:
    """Evaluate frozen point-in-time scores against strictly later total returns."""

    config = config or BacktestConfig()
    contract = _input_contract(panel)
    frame = _validate_panel(panel)
    periods: list[PeriodResult] = []
    rejected = 0
    previous_top: set[str] | None = None
    previous_bottom: set[str] | None = None
    cost_rate = config.transaction_cost_bps / 10_000.0

    for signal_date, group in frame.groupby("signal_date", sort=True):
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
        bucket_returns = group.groupby(buckets)["forward_return"].mean()
        if 0 not in bucket_returns or config.quantiles - 1 not in bucket_returns:
            rejected += 1
            continue
        quantile_returns = [
            float(bucket_returns.get(index, np.nan))
            for index in range(config.quantiles)
        ]
        top_mask = buckets == config.quantiles - 1
        bottom_mask = buckets == 0
        top_names = set(group.loc[top_mask, "_security_key"].astype(str))
        bottom_names = set(group.loc[bottom_mask, "_security_key"].astype(str))
        top_turnover = _turnover(previous_top, top_names)
        bottom_turnover = _turnover(previous_bottom, bottom_names)
        previous_top, previous_bottom = top_names, bottom_names
        top_return = float(group.loc[top_mask, "forward_return"].mean())
        bottom_return = float(group.loc[bottom_mask, "forward_return"].mean())
        gross_spread = top_return - bottom_return
        net_spread = gross_spread - cost_rate * (top_turnover + bottom_turnover)
        periods.append(
            PeriodResult(
                signal_date=pd.Timestamp(signal_date).date().isoformat(),
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
        pd.Series(np.arange(config.quantiles)).corr(
            pd.Series(mean_quantiles), method="spearman"
        )
    )
    ic_std = float(np.std(rank_ics, ddof=1)) if len(rank_ics) > 1 else 0.0
    ic_ir = (
        float(np.mean(rank_ics) / ic_std * math.sqrt(config.periods_per_year))
        if ic_std > 0
        else None
    )
    spread_std = float(np.std(net, ddof=1)) if len(net) > 1 else 0.0
    spread_sharpe = (
        float(np.mean(net) / spread_std * math.sqrt(config.periods_per_year))
        if spread_std > 0
        else None
    )
    limitations = [
        "Transaction costs are a fixed turnover charge and do not model market impact or borrow.",
        "Bootstrap intervals describe historical period variability, not future-return certainty.",
    ]
    if not contract["point_in_time_universe_attested"]:
        limitations.append(
            "The panel does not attest to a survivorship-free point-in-time universe."
        )
    if not contract["total_returns_attested"]:
        limitations.append(
            "The panel does not attest that forward_return includes distributions."
        )
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
    unique = tuple(sorted(pd.Timestamp(item) for item in pd.unique(dates)))
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

    lines = [
        "# Walk-forward score validation",
        "",
        (
            f"{len(report.periods)} accepted periods, {report.observations} observations, "
            f"{report.rejected_periods} rejected periods."
        ),
        "",
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
        (
            "| Moving-block 95% interval for mean rank IC | "
            f"{interval(report.rank_ic_interval)} |"
        ),
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
