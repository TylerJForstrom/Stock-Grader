"""Forward panel builder: join frozen score panels to realized returns.

This module closes the evidence loop. Frozen point-in-time panels accrue under
``frozen_scores/`` every month, and nothing else in the system joins them to
what the market subsequently did — so evidence accrues un-evaluated. The builder
reads a profile's frozen panels, prices each matured signal date against the
private vault's whole-market EOD archive, and emits a panel that satisfies
``backtest.py``'s strict input contract on every item it can honestly attest.

**Licensing split (ECOSYSTEM.md rule 5).** The emitted panel embeds per-row
returns derived from Massive free-tier closes ("personal use … private archive")
and stockanalysis.com delisted histories ("republication in full prohibited").
Stock-Grader is a PUBLIC repository, so the panel file itself must never be
committed here: working copies go under ``build/`` (gitignored — that is the
structural guarantee), the durable archive goes into the private vault via
:func:`archive_to_vault`, and only aggregate statistics (the backtest markdown,
the accounting counts, the ledger line) are committed publicly.

**Total returns.** ``return_is_total`` is ``False``, always, in v1. The foundry's
dividend dataset covers three tickers at fiscal-period granularity with no
ex-dates, on a current-fully-split-adjusted basis — against raw unadjusted
closes. Prorating that into a 21-day window and calling it a total return would
be a lie the attestation column exists to prevent. Flip it to True only when a
per-ex-date cash-dividend dataset (ex_date, cash_amount, same unadjusted basis)
covers >= 99% of panel rows. There is deliberately no flag that flips it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import pandas as pd

from .data.symbols import ticker_variants

__all__ = [
    "FORWARD_EPOCH",
    "PLAUSIBLE_SPLIT_RATIOS",
    "SCHEMA_VERSION",
    "PanelBuildConfig",
    "PanelBuildError",
    "PanelBuildResult",
    "PeriodAccounting",
    "archive_to_vault",
    "build_panel",
    "detect_split",
    "discover_frozen_panels",
    "load_bars",
    "select_non_overlapping",
    "trading_days",
    "window_for",
    "write_panel",
]

SCHEMA_VERSION = "1.0"

#: First genuinely forward-frozen panel on disk. ``config/universe_default.txt``
#: is a hand-picked large-cap list chosen in 2026: against a forward signal date
#: it is PIT-honest (chosen before every outcome), but a ``freeze --asof
#: 2024-01-01`` backfill against that same list is textbook survivorship. The
#: epoch refusal is the only thing stopping a future agent from manufacturing
#: fake history that looks contract-clean.
FORWARD_EPOCH = dt.date(2026, 7, 30)

#: Split ratios that occur in practice. The smallest is 1.5, so the detector can
#: only fire on a one-day move beyond roughly -33% or +50% — ordinary volatility
#: cannot trip it.
PLAUSIBLE_SPLIT_RATIOS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0)


class PanelBuildError(RuntimeError):
    """The build cannot proceed honestly (collision, duplicate id, bad input)."""


@dataclass(frozen=True, slots=True)
class PanelBuildConfig:
    horizon_days: int = 21
    min_cross_section: int = 20
    min_periods: int = 3
    max_eod_lag_days: int = 5
    max_freeze_age_days: int = 45
    include_ungraded: bool = False
    split_tolerance: float = 0.01
    max_unresolved_fraction: float = 0.02
    allow_backfilled_panels: bool = False


@dataclass(slots=True)
class PeriodAccounting:
    """Every row of a signal date accounted for — nothing silently vanishes."""

    signal_date: str
    return_start: str
    return_end: str
    frozen_rows: int = 0
    ungraded_dropped: int = 0
    missing_cik_dropped: int = 0
    no_start_price_dropped: int = 0
    resolved_market_eod: int = 0
    resolved_delisted_archive: int = 0
    resolved_last_listed_close: int = 0
    split_adjusted_foundry: int = 0
    split_adjusted_reconstructed: int = 0
    unresolved_dropped: int = 0
    kept: int = 0
    meets_min_cross_section: bool = False


@dataclass(slots=True)
class PanelBuildResult:
    panel: pd.DataFrame | None
    periods: list[PeriodAccounting] = field(default_factory=list)
    matured_signal_dates: list[str] = field(default_factory=list)
    pending_signal_dates: list[str] = field(default_factory=list)
    skipped_overlapping_signal_dates: list[str] = field(default_factory=list)
    attestations: dict[str, bool] = field(default_factory=dict)
    unresolved_rows: int = 0
    unresolved_fraction: float = 0.0
    unresolved_tickers: list[str] = field(default_factory=list)
    refusal: str | None = None
    ready_for_backtest: bool = False

    @property
    def qualifying_periods(self) -> int:
        return sum(1 for p in self.periods if p.meets_min_cross_section)


# -- calendar and period selection --------------------------------------------

def discover_frozen_panels(frozen_root: Path, profile: str) -> dict[dt.date, Path]:
    """Frozen panels for one profile, keyed and sorted by signal date.

    Only stems shaped ``YYYY-MM-DD`` are accepted (the same discipline the paper
    trader applies), so a stray temp file cannot become a signal date. The legacy
    flat ``frozen_scores/*.parquet`` layout predates the multi-profile freeze and
    is deliberately not supported.
    """
    import re

    stem = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    found: dict[dt.date, Path] = {}
    for path in sorted(Path(frozen_root, profile).glob("*.parquet")):
        if stem.fullmatch(path.stem) is None:
            continue
        found[dt.date.fromisoformat(path.stem)] = path
    return dict(sorted(found.items()))


def trading_days(vault: Any) -> list[dt.date]:
    """The archive's own coverage IS the trading calendar.

    Deliberately not a reimplemented holiday calendar: the only days a position
    can be priced are the days the archive holds, and cross-repo imports of the
    vault's holiday logic are forbidden (artifacts, not imports).
    """
    return sorted(vault.market_eod_available_days())


def window_for(
    signal: dt.date, days: list[dt.date], horizon: int
) -> tuple[dt.date, dt.date] | None:
    """Entry/exit dates for one signal, or None while it has not matured.

    Entry is the first archived day STRICTLY after the signal (the backtest
    contract requires ``return_start > signal_date``); exit is ``horizon``
    archived days after entry.
    """
    later = [d for d in days if d > signal]
    if not later:
        return None
    entry = later[0]
    entry_index = days.index(entry)
    exit_index = entry_index + horizon
    if exit_index >= len(days):
        return None
    return entry, days[exit_index]


def select_non_overlapping(
    signals: list[dt.date], days: list[dt.date], horizon: int
) -> tuple[list[dt.date], list[dt.date]]:
    """Greedy earliest-first selection of signals with disjoint outcome windows.

    An ad-hoc ``workflow_dispatch`` freeze mid-month would otherwise produce
    overlapping windows, double-counting the same market move and breaking both
    the block bootstrap's independence assumption and the ``periods_per_year=12``
    annualization.
    """
    kept: list[dt.date] = []
    skipped: list[dt.date] = []
    previous_exit: dt.date | None = None
    for signal in sorted(signals):
        window = window_for(signal, days, horizon)
        if window is None:
            continue
        entry, exit_ = window
        if previous_exit is not None and entry < previous_exit:
            skipped.append(signal)
            continue
        kept.append(signal)
        previous_exit = exit_
    return kept, skipped


# -- bulk bar loading ----------------------------------------------------------


def load_bars(vault: Any, days: Iterable[dt.date], wanted: Mapping[str, str]) -> pd.DataFrame:
    """Load bars for the wanted symbols by reading each day file exactly once.

    PERFORMANCE CONTRACT: ``VaultDataSource.market_eod_series`` reads and
    sha256-verifies every archived day file PER TICKER (~501 reads each). Reading
    each needed day once and filtering is ~250 reads for a 12-period panel
    instead of ~41,000. Cost grows linearly with retained periods, which is
    acceptable for a monthly job and bounded by the free tier's ~730-day archive.

    ``wanted`` maps every market_eod spelling variant (upper-cased) to its
    canonical panel ticker.
    """
    frames: list[pd.DataFrame] = []
    for day in sorted(set(days)):
        frame = vault.market_eod_day(day)
        if frame is None or not len(frame):
            continue
        symbols = frame["symbol"].astype(str).str.upper()
        mask = symbols.isin(wanted)
        if not mask.any():
            continue
        selected = frame.loc[mask, ["symbol", "close", "volume", "transactions"]].copy()
        selected["symbol"] = symbols[mask]
        selected["ticker"] = selected["symbol"].map(dict(wanted))
        selected["date"] = day
        frames.append(selected)
    if not frames:
        return pd.DataFrame(columns=["date", "ticker", "symbol", "close", "volume", "transactions"])
    bars = pd.concat(frames, ignore_index=True)
    # The close is nullable in the source payload; a null or non-positive close
    # is unpriceable and must not silently become a 0.0 return.
    bars = bars[pd.to_numeric(bars["close"], errors="coerce").gt(0)]
    return bars.reset_index(drop=True)


def variant_map(tickers: Iterable[str]) -> dict[str, str]:
    """Map every spelling variant to its canonical ticker; refuse collisions.

    The frozen panel uses SEC dash form (BRK-B) and market_eod uses Polygon dot
    form (BRK.B). A collision — two panel tickers claiming one archived spelling
    — would silently cross-join two issuers, so it raises instead.
    """
    wanted: dict[str, str] = {}
    for ticker in tickers:
        for variant in ticker_variants(ticker):
            claimed = wanted.get(variant)
            if claimed is not None and claimed != ticker:
                raise PanelBuildError(
                    f"symbol collision: {ticker!r} and {claimed!r} both map to archived "
                    f"spelling {variant!r}; a shared spelling would cross-join two issuers"
                )
            wanted[variant] = ticker
    return wanted


# -- split detection (three tiers, never a silent guess) -----------------------


def detect_split(
    prev_close: float,
    close: float,
    tolerance: float,
) -> float | None:
    """Return the candidate split factor when the price signature matches.

    ``factor`` is what the LATER close must be multiplied by to be comparable
    with the earlier one: 2.0 for a 2-for-1 forward split (price halves), 0.1
    for a 1-for-10 reverse split (price 10x).
    """
    if prev_close <= 0 or close <= 0:
        return None
    ratio = prev_close / close
    for plausible in PLAUSIBLE_SPLIT_RATIOS:
        if abs(ratio / plausible - 1.0) <= tolerance:
            return plausible
        if abs(ratio * plausible - 1.0) <= tolerance:
            return 1.0 / plausible
    return None


def _volume_corroborates(
    factor: float,
    prev_volume: float | None,
    volume: float | None,
    prev_transactions: float | None,
    transactions: float | None,
) -> bool:
    """A split multiplies share volume by the factor while trade count stays flat.

    A genuine -50% crash spikes BOTH volume and transactions, which is exactly
    what this test rejects. Returns False when transactions are null (verified ~3
    nulls per 12.5k rows): no corroboration without evidence.
    """
    if not prev_volume or not volume or not prev_transactions or not transactions:
        return False
    volume_ratio = volume / prev_volume
    if not (0.5 * factor <= volume_ratio <= 2.0 * factor):
        return False
    transaction_ratio = transactions / prev_transactions
    return 0.4 <= transaction_ratio <= 2.5


def _foundry_split_lookup(
    foundry_splits: pd.DataFrame | None,
    ticker: str,
    cik: str,
    *,
    day: dt.date,
    candidate: float,
    tolerance: float,
) -> float | None:
    """Tier A: a foundry split row matching ticker/CIK, date, and ratio."""
    if foundry_splits is None or foundry_splits.empty:
        return None
    variants = set(ticker_variants(ticker))
    for _, row in foundry_splits.iterrows():
        row_ticker = str(row.get("ticker", "")).upper()
        row_cik = str(row.get("cik", "")).strip().zfill(10) if row.get("cik") else ""
        if row_ticker not in variants and (not row_cik or row_cik != cik):
            continue
        effective = row.get("effective_date")
        effective_date = effective.date() if hasattr(effective, "date") else effective
        if effective_date != day:
            continue
        ratio = float(row.get("ratio", 0.0))
        if ratio <= 0:
            continue
        # A foundry ratio of R corrects a forward split: factor = R.
        for factor in (ratio, 1.0 / ratio):
            if abs(factor / candidate - 1.0) <= max(tolerance * 2, 0.02):
                return factor
    return None


def split_factor(
    ticker_bars: pd.DataFrame,
    entry: dt.date,
    exit_: dt.date,
    *,
    ticker: str,
    cik: str,
    foundry_splits: pd.DataFrame | None,
    tolerance: float,
) -> tuple[float, str, bool]:
    """Cumulative split factor over ``(entry, exit]`` for one ticker.

    Returns ``(factor, source, unresolved)`` where source is one of
    ``none | foundry | reconstructed | mixed``. ``unresolved=True`` means a
    split-shaped move was neither confirmed by the foundry nor corroborated by
    volume: the observation must be EXCLUDED and counted — keeping it fabricates
    a -50% return, dropping it silently digs a survivorship hole.
    """
    window = ticker_bars[(ticker_bars["date"] > entry) & (ticker_bars["date"] <= exit_)]
    window = pd.concat([ticker_bars[ticker_bars["date"] == entry], window]).sort_values("date")
    if len(window) < 2:
        return 1.0, "none", False
    factor = 1.0
    sources: set[str] = set()
    rows = window.to_dict("records")
    for prev, current in pairwise(rows):
        candidate = detect_split(float(prev["close"]), float(current["close"]), tolerance)
        if candidate is None:
            continue
        day = current["date"]
        confirmed = _foundry_split_lookup(
            foundry_splits, ticker, cik, day=day, candidate=candidate, tolerance=tolerance
        )
        if confirmed is not None:
            factor *= confirmed
            sources.add("foundry")
            continue
        if _volume_corroborates(
            candidate,
            prev.get("volume"),
            current.get("volume"),
            prev.get("transactions"),
            current.get("transactions"),
        ):
            factor *= candidate
            sources.add("reconstructed")
            continue
        return 1.0, "none", True  # split-shaped, uncorroborated: unresolved
    if not sources:
        return 1.0, "none", False
    source = sources.pop() if len(sources) == 1 else "mixed"
    return factor, source, False


# -- the build -----------------------------------------------------------------


def _resolve_exit_price(
    ticker_bars: pd.DataFrame,
    vault: Any,
    ticker: str,
    entry: dt.date,
    exit_: dt.date,
) -> tuple[float, str, str, bool] | None:
    """Delisting resolution chain: (close, source, symbol, terminal_price_used).

    Survivorship bias enters exactly one way — dropping a frozen row because its
    outcome is inconvenient — so no row is dropped here for an outcome-dependent
    reason. Resolution order:

    1. An archived close at the exit day (any spelling variant).
    2. The delisted archive's last close inside ``(entry, exit]`` — column ``c``,
       the raw close, never ``a`` (the site's adjusted close, undocumented basis).
    3. The last archived close in ``(entry, exit]``: the final trade on a listed
       venue before the name left it. market_eod is collected with
       ``include_otc=false``, so this convention holds the position at the last
       listed close and ignores any OTC continuation — which slightly OVERSTATES
       the return for names that kept falling off-exchange. A disclosed
       convention, not a drop.
    4. None: unresolved. The caller excludes and counts the row, and the
       panel-level ``delisting_return_included`` attestation goes False.
    """
    at_exit = ticker_bars[ticker_bars["date"] == exit_]
    if len(at_exit):
        row = at_exit.iloc[0]
        return float(row["close"]), "market_eod", str(row["symbol"]), False

    for variant in ticker_variants(ticker):
        history = vault.delisted_history(variant)
        if history is None or not len(history):
            continue
        history = history.sort_index()
        index_dates = [
            d.date() if hasattr(d, "date") else dt.date.fromisoformat(str(d))
            for d in history.index
        ]
        candidates = [
            (day, value)
            for day, value in zip(index_dates, history["c"].tolist(), strict=True)
            if entry < day <= exit_ and value and float(value) > 0
        ]
        if candidates:
            _, value = candidates[-1]
            return float(value), "delisted_archive", variant, False

    inside = ticker_bars[(ticker_bars["date"] > entry) & (ticker_bars["date"] <= exit_)]
    if len(inside):
        row = inside.sort_values("date").iloc[-1]
        return float(row["close"]), "last_listed_close", str(row["symbol"]), True

    return None


def build_panel(
    frozen_root: Path,
    profile: str,
    vault: Any,
    foundry: Any = None,
    config: PanelBuildConfig | None = None,
    *,
    today: dt.date | None = None,
) -> PanelBuildResult:
    """Join a profile's frozen panels to realized forward returns.

    Attestations are computed, never hard-coded:

    - ``universe_is_pit`` — True only when zero rows were dropped for an
      outcome-dependent reason AND every signal date is on or after
      :data:`FORWARD_EPOCH`.
    - ``delisting_return_included`` — True only when zero rows were unresolved.
    - ``return_is_total`` — False, always, in v1 (module docstring has the exact
      upgrade condition).
    """
    config = config or PanelBuildConfig()
    today = today or dt.date.today()

    panels = discover_frozen_panels(Path(frozen_root), profile)
    result = PanelBuildResult(panel=None)
    if not panels:
        result.refusal = f"no frozen panels found under {frozen_root}/{profile}"
        return result

    days = trading_days(vault)
    if not days:
        result.refusal = "the vault EOD archive is empty"
        return result

    # Freshness gates FIRST: a stale vault silently truncates maturity, and a
    # dead freeze clock must not look like "no new evidence".
    newest_eod = max(days)
    if (today - newest_eod).days > config.max_eod_lag_days:
        result.refusal = (
            f"vault EOD archive is stale: newest day {newest_eod} is more than "
            f"{config.max_eod_lag_days} days before {today}"
        )
        return result
    newest_signal = max(panels)
    if (today - newest_signal).days > config.max_freeze_age_days:
        result.refusal = (
            f"freeze clock looks dead: newest frozen signal {newest_signal} is more than "
            f"{config.max_freeze_age_days} days before {today}"
        )
        return result

    backfilled = [d for d in panels if d < FORWARD_EPOCH]
    if backfilled and not config.allow_backfilled_panels:
        result.refusal = (
            f"{len(backfilled)} frozen signal date(s) predate the forward epoch "
            f"{FORWARD_EPOCH} (earliest: {min(backfilled)}); a backfilled panel against "
            f"a hand-picked survivor universe is not point-in-time. Pass "
            f"--allow-backfilled-panels to build anyway with universe_is_pit=False."
        )
        return result

    matured: list[dt.date] = []
    pending: list[dt.date] = []
    for signal in panels:
        if window_for(signal, days, config.horizon_days) is None:
            pending.append(signal)
        else:
            matured.append(signal)
    kept, skipped = select_non_overlapping(matured, days, config.horizon_days)
    result.matured_signal_dates = [d.isoformat() for d in kept]
    result.pending_signal_dates = [d.isoformat() for d in pending]
    result.skipped_overlapping_signal_dates = [d.isoformat() for d in skipped]
    if not kept:
        return result

    # One pass over the frozen panels to learn tickers and resolve CIKs.
    frozen_frames = {signal: pd.read_parquet(panels[signal]) for signal in kept}
    foundry_universe: dict[str, str] = {}
    if foundry is not None:
        try:
            for record in foundry.universe(listed_only=False):
                ticker = str(record.get("ticker", "")).upper()
                cik = record.get("cik")
                if ticker and cik is not None and str(cik).strip().isdecimal():
                    foundry_universe.setdefault(ticker, str(cik).strip().zfill(10))
        except Exception:
            foundry_universe = {}
    foundry_splits = None
    if foundry is not None:
        try:
            foundry_splits = foundry.splits()
        except Exception:
            foundry_splits = None

    all_tickers: set[str] = set()
    for frame in frozen_frames.values():
        all_tickers.update(str(t).upper() for t in frame["ticker"].tolist())
    wanted = variant_map(all_tickers)

    needed_days: set[dt.date] = set()
    windows: dict[dt.date, tuple[dt.date, dt.date]] = {}
    for signal in kept:
        window = window_for(signal, days, config.horizon_days)
        assert window is not None  # kept implies matured
        windows[signal] = window
        entry, exit_ = window
        needed_days.update(d for d in days if entry <= d <= exit_)

    bars = load_bars(vault, needed_days, wanted)
    bars_by_ticker = dict(tuple(bars.groupby("ticker"))) if len(bars) else {}

    rows: list[dict[str, Any]] = []
    unresolved_tickers: list[str] = []
    outcome_dependent_drops = 0
    from .research_manifest import current_commit

    builder_commit = current_commit()

    for signal in kept:
        entry, exit_ = windows[signal]
        frame = frozen_frames[signal]
        accounting = PeriodAccounting(
            signal_date=signal.isoformat(),
            return_start=entry.isoformat(),
            return_end=exit_.isoformat(),
            frozen_rows=len(frame),
        )

        for _, frozen in frame.iterrows():
            ticker = str(frozen["ticker"]).upper()
            if not config.include_ungraded and not bool(frozen.get("graded", False)):
                accounting.ungraded_dropped += 1
                continue
            cik_raw = frozen.get("cik")
            cik = str(cik_raw).strip() if cik_raw is not None else ""
            if not cik or cik.lower() == "nan":
                cik = foundry_universe.get(ticker, "")
            if not cik:
                accounting.missing_cik_dropped += 1
                continue
            cik = cik.zfill(10)

            ticker_bars = bars_by_ticker.get(ticker)
            if ticker_bars is None or not len(ticker_bars):
                accounting.no_start_price_dropped += 1
                continue
            at_entry = ticker_bars[ticker_bars["date"] == entry]
            if not len(at_entry):
                # Knowable at entry: you cannot buy an unpriced name. Not an
                # outcome-dependent drop, so PIT survives — but count it.
                accounting.no_start_price_dropped += 1
                continue
            start_close = float(at_entry.iloc[0]["close"])
            entry_symbol = str(at_entry.iloc[0]["symbol"])

            factor, split_source, split_unresolved = split_factor(
                ticker_bars,
                entry,
                exit_,
                ticker=ticker,
                cik=cik,
                foundry_splits=foundry_splits,
                tolerance=config.split_tolerance,
            )
            if split_unresolved:
                accounting.unresolved_dropped += 1
                outcome_dependent_drops += 1
                unresolved_tickers.append(ticker)
                continue

            resolution = _resolve_exit_price(ticker_bars, vault, ticker, entry, exit_)
            if resolution is None:
                accounting.unresolved_dropped += 1
                outcome_dependent_drops += 1
                unresolved_tickers.append(ticker)
                continue
            end_close, return_source, price_symbol, terminal = resolution
            if return_source == "market_eod":
                accounting.resolved_market_eod += 1
            elif return_source == "delisted_archive":
                accounting.resolved_delisted_archive += 1
            else:
                accounting.resolved_last_listed_close += 1
            if split_source in ("foundry", "mixed"):
                accounting.split_adjusted_foundry += 1
            if split_source in ("reconstructed", "mixed"):
                accounting.split_adjusted_reconstructed += 1

            forward_return = (end_close * factor) / start_close - 1.0
            rows.append(
                {
                    "signal_date": signal.isoformat(),
                    "return_start": entry.isoformat(),
                    "return_end": exit_.isoformat(),
                    "ticker": ticker,
                    "score": float(frozen["score"]),
                    "forward_return": forward_return,
                    "cik": cik,
                    # The freeze fetched EDGAR ON the signal date, so no filing
                    # dated after it could have entered the feature set — the
                    # tightest bound honestly available.
                    "filed_through": signal.isoformat(),
                    "profile": str(frozen.get("profile", profile)),
                    "letter": frozen.get("letter"),
                    "percentile": frozen.get("percentile"),
                    "coverage": frozen.get("coverage"),
                    "config_fingerprint": frozen.get("config_fingerprint"),
                    "universe_fingerprint": frozen.get("universe_fingerprint"),
                    "freeze_commit": frozen.get("code_commit"),
                    "start_close": start_close,
                    "end_close": end_close,
                    "price_symbol": price_symbol,
                    "return_source": return_source,
                    "split_factor": factor,
                    "split_source": split_source,
                    "terminal_price_used": terminal,
                    "symbol_changed": price_symbol.upper() != entry_symbol.upper(),
                    "panel_schema_version": SCHEMA_VERSION,
                    "builder_commit": builder_commit,
                }
            )
            accounting.kept += 1

        accounting.meets_min_cross_section = accounting.kept >= config.min_cross_section
        result.periods.append(accounting)

    total_considered = sum(p.kept + p.unresolved_dropped for p in result.periods)
    result.unresolved_rows = sum(p.unresolved_dropped for p in result.periods)
    result.unresolved_fraction = (
        result.unresolved_rows / total_considered if total_considered else 0.0
    )
    result.unresolved_tickers = sorted(set(unresolved_tickers))

    universe_is_pit = (outcome_dependent_drops == 0) and (min(panels) >= FORWARD_EPOCH)
    result.attestations = {
        "universe_is_pit": universe_is_pit,
        "return_is_total": False,
        "delisting_return_included": result.unresolved_rows == 0,
    }

    if rows:
        panel = pd.DataFrame(rows)
        for name, value in result.attestations.items():
            panel[name] = value
        duplicate_tickers = panel.duplicated(subset=["signal_date", "ticker"], keep=False)
        duplicate_ciks = panel.duplicated(subset=["signal_date", "cik"], keep=False)
        if duplicate_tickers.any() or duplicate_ciks.any():
            offenders = panel.loc[
                duplicate_tickers | duplicate_ciks, ["signal_date", "ticker", "cik"]
            ]
            pairs = ", ".join(
                f"{r.signal_date}/{r.ticker}(cik {r.cik})" for r in offenders.itertuples()
            )
            raise PanelBuildError(
                f"duplicate security observations in one signal date: {pairs}. A dual-class "
                f"pair sharing a CIK must be resolved before the panel can be evaluated."
            )
        result.panel = panel

    result.ready_for_backtest = result.qualifying_periods >= config.min_periods
    return result


# -- writers -------------------------------------------------------------------


def sidecar_payload(result: PanelBuildResult, profile: str, config: PanelBuildConfig) -> dict:
    """Flat sidecar JSON — additive keys only, never an envelope."""
    from .research_manifest import current_commit

    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "built_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "builder_commit": current_commit(),
        "horizon_days": config.horizon_days,
        "matured_signal_dates": result.matured_signal_dates,
        "pending_signal_dates": result.pending_signal_dates,
        "skipped_overlapping_signal_dates": result.skipped_overlapping_signal_dates,
        "periods": [asdict(p) for p in result.periods],
        "qualifying_periods": result.qualifying_periods,
        "attestations": result.attestations,
        "unresolved_rows": result.unresolved_rows,
        "unresolved_fraction": result.unresolved_fraction,
        "ready_for_backtest": result.ready_for_backtest,
    }


def write_panel(
    result: PanelBuildResult, out_dir: Path, profile: str, config: PanelBuildConfig
) -> tuple[Path | None, Path]:
    """Write ``<profile>.parquet`` (stable name — the ledger's experiment key is
    derived from the panel filename) and ``<profile>.build.json``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = out_dir / f"{profile}.build.json"
    sidecar_path.write_text(
        json.dumps(sidecar_payload(result, profile, config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if result.panel is None:
        return None, sidecar_path
    panel_path = out_dir / f"{profile}.parquet"
    result.panel.to_parquet(panel_path, index=False)
    return panel_path, sidecar_path


def _vault_manifest(directory: Path, license_note: str, source_urls: list[str]) -> None:
    """Vault-shaped manifest over every non-dot, non-manifest file.

    A ~25-line copy rather than an import of ``stock_vault.manifest`` — the
    ecosystem rule is artifacts-not-imports, and this is the same precedent set
    for ``ticker_variants``. The shape must match exactly or
    ``VaultDataSource._manifest`` refuses the dataset.
    """
    files = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.name == "manifest.json" or path.is_dir():
            continue
        blob = path.read_bytes()
        files.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
            }
        )
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_urls": sorted(source_urls),
        "license_note": license_note,
        "files": files,
    }
    (directory / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n")


def archive_to_vault(
    panel_path: Path,
    sidecar_path: Path,
    archive_dir: Path,
    profile: str,
    build_date: dt.date,
) -> Path:
    """Copy the built panel into the private vault's dataset layout.

    The panel embeds per-row returns derived from restricted sources, so its
    durable home is the PRIVATE vault, never the public grader repo.
    """
    destination = Path(archive_dir) / profile
    destination.mkdir(parents=True, exist_ok=True)
    archived = destination / f"{build_date.isoformat()}.parquet"
    shutil.copyfile(panel_path, archived)
    shutil.copyfile(sidecar_path, destination / f"{build_date.isoformat()}.build.json")
    _vault_manifest(
        destination,
        license_note=(
            "Derived per-row returns from Massive (ex-Polygon) free-tier EOD closes and "
            "stockanalysis.com delisted histories; private archive, do not redistribute rows."
        ),
        source_urls=[
            "https://massive.dev/",
            "https://stockanalysis.com/",
        ],
    )
    return archived
