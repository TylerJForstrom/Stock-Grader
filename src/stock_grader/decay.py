"""Signal decay: the score's rank-IC curve across holding horizons.

Three structural honesty facts govern everything here:

1. ``backtest.py`` forbids two return windows under one signal_date, so a
   horizon sweep is N physically separate panels, never one wide file.
2. Forward returns are UNADJUSTED PRICE returns from the private vault
   (collected ``adjusted=false``) — not total returns. The panels never write
   ``return_is_total``; four of the evaluator's five contract items fail by
   construction and every run requires ``--allow-unverified-panel``.
3. Each horizon is a separate look at the same data, and is charged as a
   separate ledger trial on ONE shared denominator. Only the pre-declared
   primary horizon may pass the gate; a sweep must never promote its own
   argmax.

Outputs land under ``signal_decay/`` (gitignored): they embed vault-derived
forward returns and must never reach this public repository's history.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import logging
import math
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, BacktestReport, evaluate_walk_forward
from .significance import SignificanceReport, assess_edge, per_period_sharpe

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HORIZONS",
    "DecayConfig",
    "DecayCurve",
    "HorizonResult",
    "build_horizon_panel",
    "decay_to_markdown",
    "evaluate_decay",
    "fit_half_life",
    "load_frozen_panels",
    "record_sweep_trials",
    "write_decay_artifacts",
]

DEFAULT_HORIZONS: tuple[int, ...] = (5, 21, 63, 126)
PANEL_SCHEMA_VERSION = "1.0"
# 1.1: inference is averaged over ALL non-overlapping offset subsamples
# (Jegadeesh-Titman); decay.json embeds inference_reports_by_offset (a list)
# instead of the single fixed-offset-0 inference_report, and the curve carries
# half_life_interval. Closed dated artifact directories are never rewritten.
ARTIFACT_SCHEMA_VERSION = "1.1"

_SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0)


@dataclass(frozen=True, slots=True)
class DecayConfig:
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    primary_horizon: int = 21
    quantiles: int = 5
    min_cross_section: int = 20
    transaction_cost_bps: float = 10.0
    bootstrap_samples: int = 1_000
    seed: int = 0
    delisting_return: float | None = None  # None = drop and count
    split_screen: bool = True
    non_overlapping_only: bool = False
    # Empirically supported as NECESSARY floors — not sufficiency — by the
    # planted-IC calibration (docs/calibration/power_table_2026-08-03.md,
    # synthetic grid, production gate): below 11 periods the gate is
    # structurally closed; at 12 periods only planted rank IC >= 0.03 (1000
    # names) / 0.05 (250 names) is detected with >= 50% probability; and even
    # AT 36 periods x 250 names power is ~0.15 at IC 0.02 and ~0.95 only at
    # IC 0.05. Meeting these floors therefore must never be read as "powered
    # for realistic IC" — they only mark where the gate stops being hopeless.
    min_periods_for_power: int = 36
    min_cross_section_for_power: int = 100

    def __post_init__(self) -> None:
        horizons = tuple(self.horizons)
        if not horizons:
            raise ValueError("horizons cannot be empty")
        if list(horizons) != sorted(set(horizons)):
            raise ValueError("horizons must be strictly increasing and unique")
        if any(h < 1 or h > 504 for h in horizons):
            raise ValueError("each horizon must be in [1, 504] trading days")
        if self.primary_horizon not in horizons:
            raise ValueError(
                "primary_horizon must be one of horizons: declare which horizon you "
                "are testing BEFORE you look at the others"
            )
        if self.quantiles < 2:
            raise ValueError("quantiles must be at least 2")
        if self.min_cross_section < self.quantiles * 2:
            raise ValueError("min_cross_section must be at least 2x quantiles")
        if self.delisting_return is not None and not (
            math.isfinite(self.delisting_return) and -1.0 <= self.delisting_return <= 0.0
        ):
            raise ValueError("delisting_return must be finite and in [-1, 0]")


@dataclass(slots=True)
class HorizonResult:
    horizon_days: int
    periods: int = 0
    effective_periods: int = 0
    overlap_periods: int = 1
    observations: int = 0
    dropped_missing_exit: int = 0
    dropped_split_suspect: int = 0
    mean_rank_ic: float = float("nan")
    rank_ic_interval: tuple[float, float] | None = None
    rank_ic_information_ratio: float | None = None
    rank_ic_positive_rate: float = float("nan")
    ic_per_sqrt_day: float = float("nan")
    mean_net_spread: float = float("nan")
    annualized_spread_sharpe: float | None = None
    is_primary: bool = False
    unusable_reason: str | None = None
    descriptive: BacktestReport | None = None
    # One report per non-overlapping offset subsample (Jegadeesh-Titman); the
    # headline inference numbers are their equal-weight average, never a single
    # arbitrary phase. Empty list == horizon unusable.
    inference_by_offset: list[BacktestReport] = field(default_factory=list)
    significance: SignificanceReport | None = None


@dataclass(slots=True)
class DecayCurve:
    profile: str
    archive_through: str
    panel_origin: str
    frozen_dir: str
    signal_dates: list[str]
    config: DecayConfig
    horizons: list[HorizonResult] = field(default_factory=list)
    half_life_days: float | None = None
    half_life_interval: tuple[float, float] | None = None
    half_life_fit_r2: float | None = None
    half_life_note: str = ""
    best_horizon_by_ic_ir: int | None = None
    best_horizon_by_ic_per_sqrt_day: int | None = None
    underpowered: bool = True
    code_commit: str = "unknown"
    config_fingerprint: str = ""
    universe_fingerprint: str = ""
    ledger: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = {
            "profile": self.profile,
            "archive_through": self.archive_through,
            "panel_origin": self.panel_origin,
            "frozen_dir": self.frozen_dir,
            "signal_dates": self.signal_dates,
            "config": asdict(self.config),
            "half_life_days": self.half_life_days,
            "half_life_interval": self.half_life_interval,
            "half_life_fit_r2": self.half_life_fit_r2,
            "half_life_note": self.half_life_note,
            "best_horizon_by_ic_ir": self.best_horizon_by_ic_ir,
            "best_horizon_by_ic_per_sqrt_day": self.best_horizon_by_ic_per_sqrt_day,
            "underpowered": self.underpowered,
            "code_commit": self.code_commit,
            "config_fingerprint": self.config_fingerprint,
            "universe_fingerprint": self.universe_fingerprint,
            "ledger": self.ledger,
            "limitations": self.limitations,
            "horizons": [],
        }
        for result in self.horizons:
            entry = {
                key: getattr(result, key)
                for key in (
                    "horizon_days",
                    "periods",
                    "effective_periods",
                    "overlap_periods",
                    "observations",
                    "dropped_missing_exit",
                    "dropped_split_suspect",
                    "mean_rank_ic",
                    "rank_ic_interval",
                    "rank_ic_information_ratio",
                    "rank_ic_positive_rate",
                    "ic_per_sqrt_day",
                    "mean_net_spread",
                    "annualized_spread_sharpe",
                    "is_primary",
                    "unusable_reason",
                )
            }
            # Embed the documented reports verbatim — never re-keyed or wrapped.
            from .report import to_json

            entry["descriptive_report"] = (
                json.loads(to_json(result.descriptive)) if result.descriptive else None
            )
            entry["inference_reports_by_offset"] = [
                json.loads(to_json(report)) for report in result.inference_by_offset
            ]
            entry["significance"] = (
                json.loads(to_json(result.significance)) if result.significance else None
            )
            payload["horizons"].append(entry)
        return payload


# -- loading -------------------------------------------------------------------

_FROZEN_COLUMNS = {
    "signal_date",
    "ticker",
    "cik",
    "score",
    "letter",
    "percentile",
    "coverage",
    "graded",
    "profile",
    "config_fingerprint",
    "universe_fingerprint",
    "code_commit",
    "schema_version",
}


def load_frozen_panels(
    frozen_dir: str | Path, *, allow_fingerprint_drift: bool = False
) -> pd.DataFrame:
    """Read one PROFILE directory of frozen panels, fingerprint-gated."""
    frozen_dir = Path(frozen_dir)
    from .profiles import profile_names

    files = sorted(frozen_dir.glob("*.parquet"))
    subdirs = {p.name for p in frozen_dir.iterdir() if p.is_dir()} if frozen_dir.is_dir() else set()
    if files and subdirs & set(profile_names()):
        raise ValueError(
            f"{frozen_dir} holds both flat panels and profile subdirectories; pass the "
            f"profile subdirectory (e.g. {frozen_dir}/all_weather) so the legacy flat "
            f"panel cannot silently mix in"
        )
    if not files:
        raise ValueError(f"no frozen panels under {frozen_dir}")
    # Manifest-verified reads: a part whose bytes the sibling catalog does not
    # vouch for raises here (surfaced by cmd_decay as a per-profile refusal);
    # a directory with no manifest warns and loads — pre-convention frozen
    # dirs, including retro-backfill roots, are immutable and stay readable.
    from .frozen_manifest import verify_sibling_manifest

    for path in files:
        verify_sibling_manifest(path)
    frames = [pd.read_parquet(path) for path in files]
    frame = pd.concat(frames, ignore_index=True)
    missing = _FROZEN_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"frozen panels lack columns: {sorted(missing)}")
    versions = {str(v) for v in frame["schema_version"].unique()}
    if versions != {"1.0"}:
        raise ValueError(f"unknown frozen panel schema_version(s) {sorted(versions)}")
    profiles = set(frame["profile"].unique())
    if len(profiles) != 1:
        raise ValueError(f"frozen panels mix profiles: {sorted(profiles)}")
    frame = frame[frame["graded"].astype(bool)].copy()

    for column in ("config_fingerprint", "universe_fingerprint"):
        by_value = frame.groupby(column)["signal_date"].unique()
        if len(by_value) > 1:
            drift = "; ".join(
                f"{value[:12]}…: {', '.join(sorted(map(str, dates)))}"
                for value, dates in by_value.items()
            )
            if not allow_fingerprint_drift:
                raise ValueError(
                    f"{column} differs across signal dates ({drift}); two outputs are "
                    f"comparable only when fingerprints match. Pass "
                    f"allow_fingerprint_drift=True to accept a regime break."
                )
            log.warning("%s drift accepted: %s", column, drift)
    frame["signal_date"] = pd.to_datetime(frame["signal_date"])
    return frame.sort_values(["signal_date", "ticker"]).reset_index(drop=True)


# -- panel construction --------------------------------------------------------


def _split_suspect(closes: pd.Series) -> bool:
    values = closes.dropna().to_numpy()
    for previous, current in itertools.pairwise(values):
        if previous <= 0 or current <= 0:
            continue
        ratio = current / previous
        for k in _SPLIT_RATIOS:
            if abs(ratio - k) <= 0.02 * k or abs(ratio - 1.0 / k) <= 0.02 / k:
                return True
    return False


def build_horizon_panel(
    frozen: pd.DataFrame, closes: pd.DataFrame, horizon: int, *, config: DecayConfig
) -> tuple[pd.DataFrame, dict[str, int]]:
    """One backtest-shaped panel for one horizon.

    counts keys: dropped_missing_anchor, dropped_missing_exit,
    dropped_split_suspect, dropped_incomplete_window.
    """
    sessions = closes.index
    counts = {
        "dropped_missing_anchor": 0,
        "dropped_missing_exit": 0,
        "dropped_split_suspect": 0,
        "dropped_incomplete_window": 0,
    }
    rows: list[dict] = []
    for signal_date, group in frozen.groupby("signal_date", sort=True):
        later = sessions[sessions > signal_date]
        if not len(later):
            counts["dropped_incomplete_window"] += len(group)
            continue
        entry_i = sessions.get_loc(later[0])
        exit_i = entry_i + horizon
        if exit_i >= len(sessions):
            # The horizon has not completed: a clean drop, never a truncated
            # window — truncation would silently mix horizons.
            counts["dropped_incomplete_window"] += len(group)
            continue
        return_start = sessions[entry_i]
        return_end = sessions[exit_i]
        for row in group.itertuples():
            ticker = str(row.ticker)
            if ticker not in closes.columns:
                counts["dropped_missing_anchor"] += 1
                continue
            series = closes[ticker]
            entry_close = series.iloc[entry_i]
            exit_close = series.iloc[exit_i]
            if pd.isna(entry_close) or entry_close <= 0:
                counts["dropped_missing_anchor"] += 1
                continue
            if pd.isna(exit_close) or exit_close <= 0:
                counts["dropped_missing_exit"] += 1
                if config.delisting_return is None:
                    continue
                forward = config.delisting_return
            else:
                forward = float(exit_close) / float(entry_close) - 1.0
            if config.split_screen and _split_suspect(series.iloc[entry_i : exit_i + 1]):
                counts["dropped_split_suspect"] += 1
                continue
            rows.append(
                {
                    "signal_date": signal_date.date().isoformat(),
                    "return_start": return_start.date().isoformat(),
                    "return_end": return_end.date().isoformat(),
                    "ticker": ticker,
                    "cik": row.cik,
                    "score": float(row.score),
                    "forward_return": forward,
                    "horizon_days": horizon,
                    "entry_close": float(entry_close),
                    "exit_close": (float(exit_close) if not pd.isna(exit_close) else float("nan")),
                    "profile": row.profile,
                    "config_fingerprint": row.config_fingerprint,
                    "universe_fingerprint": row.universe_fingerprint,
                    "code_commit": row.code_commit,
                    "panel_schema_version": PANEL_SCHEMA_VERSION,
                }
            )
    panel = pd.DataFrame(rows)
    if len(panel):
        uniform = panel.groupby("signal_date")[["return_start", "return_end"]].nunique()
        if int(uniform.max().max()) != 1:
            raise ValueError("internal error: a signal_date mixes return windows")
    return panel, counts


# -- evaluation ----------------------------------------------------------------


def _offset_average(values: Sequence[float | None]) -> float | None:
    """Equal-weight mean of one statistic across offset subsamples (JT).

    None unless EVERY offset could compute the statistic — averaging only the
    offsets that happen to work would re-introduce the phase dependence this
    average exists to remove.
    """
    if not values or any(value is None or not math.isfinite(value) for value in values):
        return None
    return float(sum(values) / len(values))


def _median_spacing(signal_dates: pd.DatetimeIndex, sessions: pd.DatetimeIndex) -> int:
    if len(signal_dates) < 2:
        return 21
    positions = []
    for date in signal_dates:
        later = sessions[sessions > date]
        positions.append(sessions.get_loc(later[0]) if len(later) else len(sessions))
    gaps = np.diff(sorted(positions))
    gaps = gaps[gaps > 0]
    return int(np.median(gaps)) if len(gaps) else 21


def evaluate_decay(
    frozen_dir: str | Path,
    vault_root: str | Path,
    *,
    profile: str,
    config: DecayConfig,
    allow_fingerprint_drift: bool = False,
    archive_through: str | None = None,
) -> tuple[DecayCurve, dict[int, pd.DataFrame]]:
    from .data.vault import VaultDataSource
    from .research_manifest import current_commit

    frozen = load_frozen_panels(frozen_dir, allow_fingerprint_drift=allow_fingerprint_drift)
    vault = VaultDataSource(vault_root)
    tickers = sorted(frozen["ticker"].unique())
    end = dt.date.fromisoformat(archive_through) if archive_through else None
    closes = vault.market_eod_close_matrix(
        tickers, start=frozen["signal_date"].min().date(), end=end
    )
    if not len(closes.index):
        raise ValueError("the vault archive holds no sessions after the first signal date")
    archive_end = closes.index.max().date().isoformat()
    signal_dates = pd.DatetimeIndex(sorted(frozen["signal_date"].unique()))
    spacing = _median_spacing(signal_dates, closes.index)

    origin = "retro_backfilled" if "retro" in Path(frozen_dir).parts[-2:] else "forward_frozen"
    curve = DecayCurve(
        profile=profile,
        archive_through=archive_end,
        panel_origin=origin,
        frozen_dir=str(frozen_dir),
        signal_dates=[d.date().isoformat() for d in signal_dates],
        config=config,
        code_commit=current_commit(),
        config_fingerprint=str(frozen["config_fingerprint"].iloc[0]),
        universe_fingerprint=str(frozen["universe_fingerprint"].iloc[0]),
    )
    if vault.last_skipped_days:
        curve.limitations.append(
            f"{len(vault.last_skipped_days)} archive day(s) skipped: "
            + ", ".join(d.isoformat() for d in vault.last_skipped_days[:5])
        )

    panels: dict[int, pd.DataFrame] = {}
    usable = 0
    for horizon in config.horizons:
        result = HorizonResult(horizon_days=horizon, is_primary=horizon == config.primary_horizon)
        overlap = max(1, math.ceil(horizon / spacing))
        result.overlap_periods = overlap
        panel, counts = build_horizon_panel(frozen, closes, horizon, config=config)
        result.dropped_missing_exit = counts["dropped_missing_exit"]
        result.dropped_split_suspect = counts["dropped_split_suspect"]
        if not len(panel):
            result.unusable_reason = (
                f"no signal date has a completed {horizon}d forward window "
                f"(archive ends {archive_end})"
            )
            curve.horizons.append(result)
            continue
        panels[horizon] = panel
        periods_per_year = max(1, round(252 / spacing))
        try:
            descriptive = evaluate_walk_forward(
                panel,
                BacktestConfig(
                    quantiles=config.quantiles,
                    min_cross_section=config.min_cross_section,
                    periods_per_year=periods_per_year,
                    transaction_cost_bps=config.transaction_cost_bps,
                    bootstrap_samples=config.bootstrap_samples,
                    bootstrap_block_periods=max(3, overlap),
                    seed=config.seed,
                ),
            )
            # Jegadeesh-Titman: one non-overlapping subsample per offset phase.
            # The union uses every signal date exactly once; each subsample
            # preserves non-overlap internally. A nonempty offset that cannot
            # be evaluated fails the WHOLE horizon — averaging only the offsets
            # that happen to work would re-introduce phase dependence.
            distinct = sorted(panel["signal_date"].unique())
            inference_config = BacktestConfig(
                quantiles=config.quantiles,
                min_cross_section=config.min_cross_section,
                periods_per_year=max(1, round(periods_per_year / overlap)),
                transaction_cost_bps=config.transaction_cost_bps,
                bootstrap_samples=config.bootstrap_samples,
                bootstrap_block_periods=3,
                seed=config.seed,
            )
            offset_reports = [
                evaluate_walk_forward(
                    panel[panel["signal_date"].isin(distinct[offset::overlap])],
                    inference_config,
                )
                for offset in range(overlap)
                if distinct[offset::overlap]
            ]
        except ValueError as exc:
            result.unusable_reason = str(exc)
            curve.horizons.append(result)
            continue
        result.descriptive = descriptive
        result.inference_by_offset = offset_reports
        result.periods = len(descriptive.periods)
        result.effective_periods = int(
            round(sum(len(r.periods) for r in offset_reports) / len(offset_reports))
        )
        result.observations = int(len(panel))
        result.mean_rank_ic = float(descriptive.mean_rank_ic)
        result.rank_ic_interval = descriptive.rank_ic_interval
        result.rank_ic_positive_rate = float(descriptive.rank_ic_positive_rate)
        if config.non_overlapping_only:
            # Headline descriptive columns from the offset-averaged
            # non-overlapping view. The interval becomes the ENVELOPE across
            # offsets: the offsets share overlapping returns, so averaging
            # correlated estimates must never be presented as a tighter CI.
            mean_ics = [r.mean_rank_ic for r in offset_reports]
            positive_rates = [r.rank_ic_positive_rate for r in offset_reports]
            result.mean_rank_ic = (
                float(sum(mean_ics) / len(mean_ics))
                if all(math.isfinite(v) for v in mean_ics)
                else float("nan")
            )
            result.rank_ic_positive_rate = (
                float(sum(positive_rates) / len(positive_rates))
                if all(math.isfinite(v) for v in positive_rates)
                else float("nan")
            )
            intervals = [r.rank_ic_interval for r in offset_reports]
            result.rank_ic_interval = (
                (
                    min(interval[0] for interval in intervals),
                    max(interval[1] for interval in intervals),
                )
                if intervals and all(interval is not None for interval in intervals)
                else None
            )
        result.rank_ic_information_ratio = _offset_average(
            [r.rank_ic_information_ratio for r in offset_reports]
        )
        net_spreads = [r.mean_net_spread for r in offset_reports]
        result.mean_net_spread = (
            float(sum(net_spreads) / len(net_spreads))
            if all(math.isfinite(v) for v in net_spreads)
            else float("nan")
        )
        result.annualized_spread_sharpe = _offset_average(
            [r.annualized_spread_sharpe for r in offset_reports]
        )
        result.ic_per_sqrt_day = (
            float(result.mean_rank_ic) / math.sqrt(horizon)
            if math.isfinite(result.mean_rank_ic)
            else float("nan")
        )
        curve.horizons.append(result)
        usable += 1

    if usable == 0:
        newest = signal_dates.max().date().isoformat()
        shortest = min(config.horizons)
        raise ValueError(
            f"no horizon has a completed forward window: the archive ends "
            f"{archive_end} and the newest signal date is {newest}; the shortest "
            f"horizon ({shortest}d) needs more archived sessions"
        )

    (
        curve.half_life_days,
        curve.half_life_interval,
        curve.half_life_fit_r2,
        curve.half_life_note,
    ) = fit_half_life(curve.horizons)
    by_ir = [
        (r.rank_ic_information_ratio, r.horizon_days)
        for r in curve.horizons
        if r.rank_ic_information_ratio is not None
    ]
    curve.best_horizon_by_ic_ir = max(by_ir)[1] if by_ir else None
    by_sqrt = [
        (r.ic_per_sqrt_day, r.horizon_days)
        for r in curve.horizons
        if math.isfinite(r.ic_per_sqrt_day)
    ]
    curve.best_horizon_by_ic_per_sqrt_day = max(by_sqrt)[1] if by_sqrt else None

    cross_sections = [
        int(np.median([p.n_securities for p in r.descriptive.periods]))
        for r in curve.horizons
        if r.descriptive is not None and r.descriptive.periods
    ]
    curve.underpowered = any(
        r.descriptive is not None and len(r.descriptive.periods) < config.min_periods_for_power
        for r in curve.horizons
    ) or (bool(cross_sections) and min(cross_sections) < config.min_cross_section_for_power)

    curve.limitations.extend(
        [
            (
                "forward returns are unadjusted price returns (no dividends, no delisting "
                "proceeds); return_is_total is unattestable"
            ),
            "the universe is today's survivors; universe_is_pit is unattestable",
            (
                f"split screen dropped {sum(r.dropped_split_suspect for r in curve.horizons)} "
                f"observation(s); missing exits dropped "
                f"{sum(r.dropped_missing_exit for r in curve.horizons)}"
            ),
        ]
    )
    return curve, panels


_Z95 = 1.959963984540054  # two-sided 95% normal quantile


def fit_half_life(
    results: Sequence[HorizonResult],
) -> tuple[float | None, tuple[float, float] | None, float | None, str]:
    """Inverse-variance-weighted OLS of ln(mean IC) on horizon.

    Returns ``(half_life_days, half_life_interval, r2, note)``. Each point is
    weighted by the precision implied by its per-horizon moving-block-bootstrap
    IC interval (delta method into log space), so one noisy long-horizon point
    cannot steer the fit; the 95% half-life interval propagates the slope's
    known-variance standard error. When any usable horizon lacks a usable
    interval the fit falls back to unweighted and reports no interval rather
    than inventing precision. Refuses whenever the fit cannot support a
    half-life: fewer than 3 positive-IC points, non-decaying IC, or poor fit.
    """
    points = [
        (r.horizon_days, r.mean_rank_ic, r.rank_ic_interval)
        for r in results
        if r.unusable_reason is None and math.isfinite(r.mean_rank_ic) and r.mean_rank_ic > 0
    ]
    if len(points) < 3:
        return None, None, None, "too few positive-IC horizons to fit a decay curve"
    x = np.array([p[0] for p in points], dtype=float)
    y = np.log(np.array([p[1] for p in points], dtype=float))

    log_variances: list[float] | None = []
    for _, mean_ic, interval in points:
        if interval is None:
            log_variances = None
            break
        ic_se = (interval[1] - interval[0]) / (2.0 * _Z95)
        if not (math.isfinite(ic_se) and ic_se > 0.0):
            log_variances = None
            break
        log_variances.append((ic_se / mean_ic) ** 2)  # delta method: Var[ln IC]
    weighted = log_variances is not None
    w = 1.0 / np.array(log_variances, dtype=float) if weighted else np.ones_like(x)

    s_w = float(np.sum(w))
    s_x = float(np.sum(w * x))
    s_y = float(np.sum(w * y))
    s_xx = float(np.sum(w * x * x))
    s_xy = float(np.sum(w * x * y))
    denominator = s_w * s_xx - s_x * s_x
    slope = (s_w * s_xy - s_x * s_y) / denominator
    intercept = (s_xx * s_y - s_x * s_xy) / denominator
    predicted = intercept + slope * x
    ss_res = float(np.sum(w * (y - predicted) ** 2))
    ss_tot = float(np.sum(w * (y - s_y / s_w) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    lambda_ = -float(slope)
    if lambda_ <= 0:
        return (
            None,
            None,
            r2,
            (
                f"IC does not decay across the horizons tested; the natural holding period "
                f"is at or beyond {max(p[0] for p in points)}d — extend the sweep before "
                f"concluding"
            ),
        )
    if r2 < 0.5:
        return (
            None,
            None,
            r2,
            (
                f"exponential decay fits poorly (R2={r2:.2f}); the points do not describe "
                f"a clean decay"
            ),
        )
    half_life = math.log(2) / lambda_
    if not weighted:
        return (
            half_life,
            None,
            r2,
            (
                "exponential fit accepted (unweighted: a per-horizon IC interval is "
                "unavailable, so no half-life interval is reported)"
            ),
        )
    # With known per-point variances the slope's variance is Sw / denominator.
    lambda_se = math.sqrt(s_w / denominator)
    if lambda_ - _Z95 * lambda_se <= 0.0:
        return (
            half_life,
            None,
            r2,
            (
                "exponential fit accepted (inverse-variance weighted); the decay rate "
                "is not distinguishable from zero at 95%, so the half-life interval "
                "is unbounded above and not reported"
            ),
        )
    interval = (
        math.log(2) / (lambda_ + _Z95 * lambda_se),
        math.log(2) / (lambda_ - _Z95 * lambda_se),
    )
    return half_life, interval, r2, "exponential fit accepted (inverse-variance weighted)"


# -- ledger --------------------------------------------------------------------


def record_sweep_trials(curve: DecayCurve, *, ledger_path: Path, alpha: float = 0.05) -> dict:
    """Every horizon is its own trial, on ONE order-independent denominator.

    Raises ``ValueError`` on a ledger whose chain does not verify: this
    verb reads the trial denominator AND extends the chain, exactly like
    ``backtest``, and every appending verb in the CLI already refuses a broken
    chain. ``stock-grader decay`` is documented as a hand-run command, so the
    workflow-level chain gate does not cover it.
    """
    from .research_manifest import (
        ResearchRecord,
        append_record,
        current_commit,
        load_manifest,
        verify_chain,
    )
    from .research_manifest import trial_sharpes as trial_sharpes_from

    ledger_path = Path(ledger_path)
    records = load_manifest(ledger_path) if ledger_path.exists() else []
    if not verify_chain(records):
        raise ValueError(
            f"{ledger_path} does not verify; refusing to evaluate against a broken "
            f"chain or append to it"
        )
    prior = trial_sharpes_from(records)
    usable = [r for r in curve.horizons if r.inference_by_offset]
    sweep_sharpes = []
    spreads_by_horizon: dict[int, list[list[float]]] = {}
    trial_sharpe_by_horizon: dict[int, float | None] = {}
    for result in usable:
        # One non-overlapping net-spread series per offset (JT). The trial
        # Sharpe is their equal-weight average — every period used once, no
        # arbitrary phase — and only exists when EVERY offset can compute one.
        per_offset = [
            [p.net_spread for p in report.periods] for report in result.inference_by_offset
        ]
        spreads_by_horizon[result.horizon_days] = per_offset
        offset_sharpes = [per_period_sharpe(spreads) for spreads in per_offset]
        trial_sharpe = (
            float(sum(offset_sharpes) / len(offset_sharpes))
            if all(len(spreads) >= 2 for spreads in per_offset)
            and all(math.isfinite(sharpe) for sharpe in offset_sharpes)
            else None
        )
        trial_sharpe_by_horizon[result.horizon_days] = trial_sharpe
        if trial_sharpe is not None:
            sweep_sharpes.append(trial_sharpe)
    trial_sharpes = [*prior, *sweep_sharpes]
    n_denominator = len(trial_sharpes)

    failed_contract = [
        "point_in_time_universe_attested",
        "total_returns_attested",
        "delistings_included_attested",
        "filing_cutoff_provided",
    ]
    record_hashes: dict[int, str] = {}
    deflated_benchmark = None
    for result in sorted(usable, key=lambda r: r.horizon_days):
        per_offset = spreads_by_horizon[result.horizon_days]
        periods_per_year = (
            result.inference_by_offset[0].config.periods_per_year
            if hasattr(result.inference_by_offset[0], "config")
            else 12
        )
        if all(len(spreads) >= 2 for spreads in per_offset):
            # One honest assessment per offset; record the MOST CONSERVATIVE
            # (a failing offset first, then lowest DSR, then lowest CI floor),
            # so the gate can pass only when no phase choice could flip it.
            # Averaging correlated offsets must never tighten a CI.
            assessments = [
                assess_edge(
                    spreads,
                    trial_sharpes,
                    periods_per_year=periods_per_year,
                    bootstrap_seed=curve.config.seed,
                )
                for spreads in per_offset
            ]
            significance = min(
                assessments,
                key=lambda a: (a.significant, a.deflated_sharpe, a.sharpe_ci_low),
            )
        else:
            significance = None
        result.significance = significance
        verdict_core = significance.verdict if significance else "INSUFFICIENT SAMPLE"
        gate = bool(result.is_primary and significance and significance.significant)
        prefix = (
            "PRIMARY (pre-declared)" if result.is_primary else "EXPLORATORY (non-primary horizon)"
        )
        overlap = result.overlap_periods
        record = ResearchRecord(
            experiment=f"signal_decay:{curve.profile}:h{result.horizon_days}",
            market="us_equities",
            symbols=[],
            targets=["forward_return"],
            horizons=[result.horizon_days],
            trials=n_denominator,
            metrics={
                "per_period_sharpe": trial_sharpe_by_horizon[result.horizon_days],
                "mean_net_spread": (
                    result.mean_net_spread if math.isfinite(result.mean_net_spread) else None
                ),
                "mean_rank_ic": (
                    result.mean_rank_ic if math.isfinite(result.mean_rank_ic) else None
                ),
                "deflated_sharpe": (
                    significance.deflated_sharpe
                    if significance and math.isfinite(significance.deflated_sharpe)
                    else None
                ),
                "rank_ic_information_ratio": result.rank_ic_information_ratio,
                "ic_per_sqrt_day": (
                    result.ic_per_sqrt_day if math.isfinite(result.ic_per_sqrt_day) else None
                ),
                "effective_periods": float(result.effective_periods),
            },
            costs={
                "transaction_cost_bps": float(curve.config.transaction_cost_bps),
                "delisting_return": float(curve.config.delisting_return or 0.0),
            },
            benchmark="zero",
            leakage_controls=(
                f"horizon sweep {list(curve.config.horizons)}d, "
                f"primary={curve.config.primary_horizon}d; one trial per horizon on a "
                f"shared denominator of {n_denominator}; inference averaged over all "
                f"{len(result.inference_by_offset)} non-overlapping offset subsamples "
                f"(stride {overlap}, Jegadeesh-Titman) — offsets share overlapping "
                f"returns, so averaging removes the arbitrary phase but must never "
                f"tighten a CI; significance is the most conservative offset; E[max] "
                f"deflation assumes INDEPENDENT trials — horizons of one score are "
                f"highly correlated, so this correction OVER-deflates; the remedy is a "
                f"pre-declared primary horizon, not a private discount; panel contract: "
                f"FAILED {','.join(failed_contract)}"
            ),
            gate_passed=gate,
            verdict=f"{prefix} -- {verdict_core}",
            data_span=f"{curve.signal_dates[0]}..{curve.signal_dates[-1]}",
            code_commit=current_commit(),
        )
        # The chained record's hash, not the pre-append object's: prev_sha256
        # is inside the hashed payload, so they always differ, and this one is
        # baked into an immutable dated decay.json whose ledger.record_hashes
        # must reconcile with the ledger forever.
        written = append_record(ledger_path, record)
        record_hashes[result.horizon_days] = written.integrity_sha256()
        if significance is not None:
            deflated_benchmark = significance.deflated_benchmark_sr

    return {
        "path": str(ledger_path),
        "prior_trials": len(prior),
        "trials_added": len(sweep_sharpes),
        "lifetime_trials": n_denominator,
        "deflated_benchmark_sr": deflated_benchmark,
        "record_hashes": {str(k): v for k, v in record_hashes.items()},
    }


# -- artifacts -----------------------------------------------------------------


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def write_decay_artifacts(
    curve: DecayCurve, panels: dict[int, pd.DataFrame], out_root: str | Path
) -> Path:
    out_dir = Path(out_root) / curve.profile / curve.archive_through
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []

    for horizon, panel in sorted(panels.items()):
        name = f"panel_h{horizon:03d}.parquet"
        path = out_dir / name
        tmp = path.with_suffix(".parquet.tmp")
        panel.to_parquet(tmp, index=False)
        tmp.replace(path)
        blob = path.read_bytes()
        files.append(
            {
                "name": name,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
                "rows": int(len(panel)),
            }
        )

    decay_json = json.dumps(curve.to_dict(), indent=2, sort_keys=True, default=str) + "\n"
    _atomic_write(out_dir / "decay.json", decay_json.encode("utf-8"))
    files.append(
        {
            "name": "decay.json",
            "sha256": hashlib.sha256(decay_json.encode("utf-8")).hexdigest(),
            "bytes": len(decay_json.encode("utf-8")),
        }
    )
    markdown = decay_to_markdown(curve)
    _atomic_write(out_dir / "decay.md", markdown.encode("utf-8"))
    files.append(
        {
            "name": "decay.md",
            "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "bytes": len(markdown.encode("utf-8")),
        }
    )

    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "generated_at_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "code_commit": curve.code_commit,
        "profile": curve.profile,
        "archive_through": curve.archive_through,
        "panel_origin": curve.panel_origin,
        "primary_horizon": curve.config.primary_horizon,
        "horizons": list(curve.config.horizons),
        "config_fingerprint": curve.config_fingerprint,
        "universe_fingerprint": curve.universe_fingerprint,
        "files": files,
        "license_note": (
            "PRIVATE — forward returns derived from the Stock-Vault archive "
            "(Massive/ex-Polygon, personal-use licence). Do not publish or commit to "
            "a public repository."
        ),
    }
    _atomic_write(
        out_dir / "manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )

    probe = subprocess.run(
        ["git", "check-ignore", "-q", str(Path(out_root))],
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0:
        print(
            f"\x1b[31mWARNING: {out_root} is NOT gitignored and contains restricted "
            f"vendor-derived data — do not commit it.\x1b[0m"
        )
    return out_dir


# -- markdown ------------------------------------------------------------------


def _num(value, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}"


def _pct(value) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.2%}"


def decay_to_markdown(curve: DecayCurve) -> str:
    from .report import DISCLAIMER

    lines = [
        f"# Signal decay — {curve.profile}",
        "",
        f"> {DISCLAIMER}",
        "",
        (
            f"- **Archive through**: {curve.archive_through}   "
            f"**Origin**: {curve.panel_origin}   **Commit**: `{curve.code_commit}`"
        ),
        (
            f"- **Config**: `{curve.config_fingerprint[:12]}`   "
            f"**Universe**: `{curve.universe_fingerprint[:12]}`"
        ),
        (
            f"- **Signal dates**: {len(curve.signal_dates)} "
            f"({curve.signal_dates[0]} .. {curve.signal_dates[-1]})"
        ),
        "",
        (
            "**What this is not.** The universe is today's survivors, returns are "
            "unadjusted price returns (no dividends, no delisting proceeds), and no "
            "filing cutoff is proven — four of the evaluator's five contract items fail "
            "by construction, which is why every run requires `--allow-unverified-panel`."
        ),
        "",
        (
            "| horizon | periods | eff. | overlap | obs | mean IC | IC 95% | IC IR | "
            "IC>0 | IC/√d | net spread | Sharpe | dropped exit/split | trial |"
        ),
        "|---:|---:|---:|---:|---:|---:|:---:|---:|---:|---:|---:|---:|:---:|:---|",
    ]
    for result in curve.horizons:
        if result.unusable_reason is not None:
            lines.append(
                f"| {result.horizon_days}d | — | — | — | — | — | — | — | — | — | — | — "
                f"| — | unusable: {result.unusable_reason} |"
            )
            continue
        interval = (
            f"[{result.rank_ic_interval[0]:.3f}, {result.rank_ic_interval[1]:.3f}]"
            if result.rank_ic_interval
            else "—"
        )
        lines.append(
            f"| {result.horizon_days}d | {result.periods} | {result.effective_periods} "
            f"| {result.overlap_periods} | {result.observations} "
            f"| {_num(result.mean_rank_ic)} | {interval} "
            f"| {_num(result.rank_ic_information_ratio)} "
            f"| {_pct(result.rank_ic_positive_rate)} | {_num(result.ic_per_sqrt_day, 4)} "
            f"| {_pct(result.mean_net_spread)} | {_num(result.annualized_spread_sharpe)} "
            f"| {result.dropped_missing_exit}/{result.dropped_split_suspect} "
            f"| {'PRIMARY' if result.is_primary else 'exploratory'} |"
        )

    usable = [r for r in curve.horizons if r.unusable_reason is None]
    if usable:
        lines.append("")
        peak = max(abs(r.mean_rank_ic) for r in usable if math.isfinite(r.mean_rank_ic))
        for result in usable:
            if not math.isfinite(result.mean_rank_ic) or peak == 0:
                continue
            width = int(18 * abs(result.mean_rank_ic) / peak)
            bar = "#" * max(width, 1)
            sign = "+" if result.mean_rank_ic >= 0 else "-"
            lines.append(
                f"    {result.horizon_days:>4}d | {bar:<18} | {sign}{abs(result.mean_rank_ic):.3f}"
            )

    if curve.half_life_days is None:
        half_life_line = f"Half-life: not reported — {curve.half_life_note}."
    else:
        interval_text = (
            f"95% interval [{curve.half_life_interval[0]:.0f}, "
            f"{curve.half_life_interval[1]:.0f}]d, "
            if curve.half_life_interval is not None
            else ""
        )
        half_life_line = (
            f"Half-life: **{curve.half_life_days:.0f} trading days** "
            f"({interval_text}R² {curve.half_life_fit_r2:.2f}; {curve.half_life_note})."
        )
    lines += [
        "",
        (
            "Inference columns (IC IR, net spread, Sharpe) are Jegadeesh–Titman "
            "averages over all non-overlapping offset subsamples, and `eff.` is the "
            "mean periods per offset. The offsets share overlapping returns, so the "
            "average removes the arbitrary phase choice but never tightens a CI; "
            "significance uses the most conservative offset."
        ),
        "",
        "## Reading the curve",
        "",
        half_life_line,
        "",
        (
            f"Best horizon by IC IR: {curve.best_horizon_by_ic_ir or '—'}d; by IC/√d: "
            f"{curve.best_horizon_by_ic_per_sqrt_day or '—'}d. Under a fixed rebalancing "
            "budget the argmax of IC/√days is the holding-period estimate; both argmaxes "
            "are post-hoc over this same sweep and neither clears a gate."
        ),
        "",
        "## Multiple-testing charge",
        "",
        (
            f"Prior trials {curve.ledger.get('prior_trials', 0)}, added "
            f"{curve.ledger.get('trials_added', 0)}, lifetime "
            f"{curve.ledger.get('lifetime_trials', 0)}; deflated benchmark Sharpe "
            f"{_num(curve.ledger.get('deflated_benchmark_sr'), 4)}. The E[max] correction "
            "assumes independent trials, but the horizons of one score are highly "
            "correlated, so the correction over-deflates; the remedy is the pre-declared "
            "primary horizon, never a private discount."
        ),
    ]
    if curve.ledger.get("prior_trials", 0) == 0 and curve.ledger.get("trials_added", 0) <= 1:
        lines.append(
            "\nOnly one trial exists, so the E[max] benchmark is 0 and NOTHING was "
            "deflated — do not read this as having survived the correction."
        )
    lines += [
        "",
        "## Power",
        "",
        (
            f"**Underpowered.** The floors are >= {curve.config.min_periods_for_power} "
            f"periods and >= {curve.config.min_cross_section_for_power} names per "
            f"cross-section; below 30 observations `assess_edge` reports INSUFFICIENT "
            f"SAMPLE regardless of the numbers above."
            if curve.underpowered
            else "Power floors met."
        ),
        "",
        "## Limitations",
        "",
    ]
    lines += [f"- {item}" for item in curve.limitations]
    return "\n".join(lines) + "\n"
