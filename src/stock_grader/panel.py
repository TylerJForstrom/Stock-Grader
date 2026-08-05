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

**Total returns.** v1 hard-coded ``return_is_total=False``: the foundry's XBRL
dividend dataset (then three tickers; now ~2,800 dividend payers from the
universe-wide corporate-actions sweep, but still fiscal-period totals with no
ex-dates — granularity, not coverage, is the durable disqualifier) could not
place cash inside a return window, and this docstring promised to flip only
when a per-ex-date cash-dividend dataset (ex_date, cash_amount, same
unadjusted basis) covers >= 99% of panel rows. The vault's ``data/dividends/`` archive (Massive
reference dividends: as-declared cash per share, the same unadjusted basis as
its ``adjusted=false`` closes) satisfies that data shape, so the attestation is
now COMPUTED per build: ``forward_return`` becomes ``(P_end * split_factor +
sum(in-window cash)) / P_start - 1`` and ``return_is_total`` goes True only
when the measured per-row dividend coverage is >=
:data:`TOTAL_RETURN_COVERAGE_BAR`. There is still deliberately no flag that
flips it — a vault without the archive, or thin coverage, keeps it False, and
the measured coverage is recorded in the sidecar either way.

**Dividend window convention.** A row's cash window is ``(entry, exit_]`` —
entry-exclusive, exit-inclusive — matching both ``window_for``'s return
semantics (buy at entry close, sell at exit close) and ``split_factor``'s
window exactly. A dividend going ex ON the entry day belongs to the seller:
buying at that day's close buys ex-dividend. A dividend going ex ON the exit
day is received: the exit close is already ex, but the share was held through
the ex-date open. A row is dividend-covered only when every calendar month its
window touches is archived AND the cash can be placed on the entry share
basis; a row with a mid-window split and in-window dividends stays price-only
and counts uncovered (the cumulative window factor cannot say whether each
ex-date fell before or after the split day), as does non-USD cash against USD
closes. Dividends are never delisting proceeds:
``delisting_return_included`` is computed exactly as before, unchanged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
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
    "TOTAL_RETURN_COVERAGE_BAR",
    "PanelBuildConfig",
    "PanelBuildError",
    "PanelBuildResult",
    "PeriodAccounting",
    "archive_to_vault",
    "build_panel",
    "detect_split",
    "discover_frozen_panels",
    "foundry_splits_in_window",
    "load_bars",
    "load_dividend_events",
    "resolve_exit_price",
    "select_non_overlapping",
    "trading_days",
    "window_for",
    "window_months",
    "write_panel",
    "write_vault_manifest",
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
#: cannot trip it. That floor is deliberate and must NOT be lowered: it is what
#: stops a bad day from being mistaken for a corporate action. It is also why
#: the price signature can never be the DETECTOR for a 5:4, 6:5 or 1.2:1 split —
#: see :func:`split_factor`, which reads the foundry's authoritative table first.
PLAUSIBLE_SPLIT_RATIOS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0)

#: Widest ratio the foundry table may assert before this code treats the row as
#: unusable rather than authoritative. ``splits.jsonl`` carries parse artifacts
#: spanning ~3e-06 to ~1.1e8; a 50:1 split is already extraordinary, and nothing
#: beyond this band is a ratio a return may be multiplied by.
FOUNDRY_SPLIT_RATIO_BAND = (1.0 / 50.0, 50.0)

#: How far the observed one-day price ratio may sit from a foundry-recorded
#: split before the two are treated as contradicting each other. A real split
#: moves the close by the ratio plus that day's ordinary return, so a tight band
#: still admits genuine events; wider than this and a flat price would accept a
#: 1.25 ratio, fabricating the very return this tier exists to prevent.
#: Contradiction is UNRESOLVED (dropped and counted), never a silent 1.0.
FOUNDRY_SPLIT_PRICE_TOLERANCE = 0.10

#: The module docstring's own historical bar for attesting total returns: the
#: per-ex-date dividend archive must honestly cover at least this fraction of
#: kept panel rows. A constant, not a config knob — there must be no switch
#: that makes the panel claim something untrue.
TOTAL_RETURN_COVERAGE_BAR = 0.99


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
    # Rows whose CIK came from the foundry fallback map rather than the
    # frozen panel itself — the rows the per-date PIT resolution protects.
    cik_from_foundry: int = 0
    no_start_price_dropped: int = 0
    resolved_market_eod: int = 0
    resolved_delisted_archive: int = 0
    resolved_last_listed_close: int = 0
    split_adjusted_foundry: int = 0
    split_adjusted_reconstructed: int = 0
    unresolved_dropped: int = 0
    kept: int = 0
    meets_min_cross_section: bool = False
    # Dividend accounting: covered + uncovered == kept, always.
    dividend_covered: int = 0
    dividend_uncovered: int = 0
    dividend_cash_rows: int = 0


@dataclass(slots=True)
class PanelBuildResult:
    panel: pd.DataFrame | None
    periods: list[PeriodAccounting] = field(default_factory=list)
    matured_signal_dates: list[str] = field(default_factory=list)
    pending_signal_dates: list[str] = field(default_factory=list)
    skipped_overlapping_signal_dates: list[str] = field(default_factory=list)
    attestations: dict[str, bool] = field(default_factory=dict)
    # Which foundry CIK map served each kept signal date: "pit_replay"
    # (symbol events replayed to the date), "current_snapshot" (replay
    # unavailable — the honest best available), or "unavailable" (no foundry
    # map at all). Recorded in the sidecar; deliberately NOT an attestation —
    # ``universe_is_pit`` keeps its documented, computed meaning.
    cik_map_modes: dict[str, str] = field(default_factory=dict)
    unresolved_rows: int = 0
    unresolved_fraction: float = 0.0
    unresolved_tickers: list[str] = field(default_factory=list)
    dividend_coverage: float = 0.0
    dividend_archive_months: int = 0
    # Did EVERY frozen part consumed verify against a sibling manifest? A
    # missing manifest used to warn and load, and the boolean was discarded, so
    # an unattested build was byte-indistinguishable downstream from an
    # attested one. It is recorded here and gates ready_for_backtest: a panel
    # nothing attests must not become forward evidence.
    frozen_inputs_attested: bool = True
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


# -- per-ex-date cash dividends ------------------------------------------------


def load_dividend_events(
    vault: Any, start: dt.date, end: dt.date, wanted: Mapping[str, str]
) -> tuple[dict[str, list[tuple[dt.date, float, str]]], frozenset[str]]:
    """Vault dividend archive as per-canonical-ticker events, plus archived months.

    Returns ``(events, months)`` where ``events[ticker]`` is a list of
    ``(ex_date, cash_amount, currency)`` and ``months`` is the set of archived
    ``YYYY-MM`` ex-date months. Both are empty when the vault clone predates
    the dividend collector — the no-archive path must behave exactly like the
    price-only v1 build, never guess. Provider tickers join through the same
    ``wanted`` spelling-variant map the bars use, so a dot-form archive
    spelling lands on its dash-form panel ticker and never on a stranger.
    """
    months_of = getattr(vault, "dividend_months", None)
    fetch = getattr(vault, "dividends", None)
    if months_of is None or fetch is None:
        return {}, frozenset()
    months = frozenset(months_of())
    if not months:
        return {}, frozenset()
    events: dict[str, list[tuple[dt.date, float, str]]] = {}
    frame = fetch(start, end)
    if frame is not None and len(frame):
        for row in frame.itertuples(index=False):
            ticker = wanted.get(str(row.ticker).upper())
            if ticker is None:
                continue
            events.setdefault(ticker, []).append(
                (row.ex_dividend_date, float(row.cash_amount), str(row.currency or "").upper())
            )
    return events, months


def window_months(entry: dt.date, exit_: dt.date) -> set[str]:
    """Calendar months the dividend window ``(entry, exit_]`` touches."""
    cursor = (entry + dt.timedelta(days=1)).replace(day=1)
    months: set[str] = set()
    while cursor <= exit_:
        months.add(cursor.strftime("%Y-%m"))
        cursor = (cursor + dt.timedelta(days=32)).replace(day=1)
    return months


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


def foundry_splits_in_window(
    foundry_splits: pd.DataFrame | None,
    ticker: str,
    cik: str,
    *,
    entry: dt.date,
    exit_: dt.date,
) -> tuple[list[tuple[dt.date, float]], list[tuple[dt.date, float]]]:
    """Tier A as a DETECTOR: every foundry split effective in ``(entry, exit]``.

    Returns ``(usable, implausible)`` — both lists of ``(effective_date,
    ratio)``, oldest first. ``usable`` ratios sit inside
    :data:`FOUNDRY_SPLIT_RATIO_BAND`; ``implausible`` ones do not, and a caller
    must treat those as unresolved rather than as an absence of a split: the
    table asserts an event, and a ratio this code will not multiply by is
    missing information, not a factor of 1.0.

    This is the inversion that matters. The foundry table is authoritative,
    carries ``effective_date`` and ``ratio`` for every split, and used to be
    reachable ONLY after the price signature had already guessed a ratio — so
    it could confirm, never detect. Every ratio between 1/1.5 and 1.5 (5:4,
    6:5, 1.2:1, and their reverse counterparts) was therefore invisible in both
    directions: the row survived with ``split_factor=1.0``, its forward return
    fabricated by the whole size of the split, and — because the dividend leg
    keyed off that same 1.0 — was also marked dividend-covered.
    """
    usable: list[tuple[dt.date, float]] = []
    implausible: list[tuple[dt.date, float]] = []
    if foundry_splits is None or foundry_splits.empty:
        return usable, implausible
    variants = set(ticker_variants(ticker))
    low, high = FOUNDRY_SPLIT_RATIO_BAND
    for _, row in foundry_splits.iterrows():
        row_ticker = str(row.get("ticker", "")).upper()
        row_cik = str(row.get("cik", "")).strip().zfill(10) if row.get("cik") else ""
        if row_ticker not in variants and (not row_cik or row_cik != cik):
            continue
        effective = row.get("effective_date")
        effective_date = effective.date() if hasattr(effective, "date") else effective
        if not isinstance(effective_date, dt.date):
            continue
        if not (entry < effective_date <= exit_):
            continue
        try:
            ratio = float(row.get("ratio", 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ratio) or ratio <= 0 or ratio == 1.0:
            continue
        (usable if low <= ratio <= high else implausible).append((effective_date, ratio))
    usable.sort()
    implausible.sort()
    return usable, implausible


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
    split could not be resolved honestly: the observation must be EXCLUDED and
    counted — keeping it fabricates a return, dropping it silently digs a
    survivorship hole.

    Two tiers, in this order:

    1. **Foundry (detector).** Every split the authoritative table records as
       effective in ``(entry, exit]`` is applied, whatever its ratio. Each is
       attributed to the bar pair whose interval contains its effective date
       (so a halted or missing session still lands on the gap it caused) and
       arbitrated against the observed move, in the orientation closest to it;
       a foundry ratio the price series contradicts by more than
       :data:`FOUNDRY_SPLIT_PRICE_TOLERANCE` is unresolved, never applied and
       never ignored. A recorded split whose ratio is outside
       :data:`FOUNDRY_SPLIT_RATIO_BAND`, or that this window has no bar pair to
       place it against, is likewise unresolved.
    2. **Price signature + volume corroboration (fallback).** For pairs the
       foundry does not cover: :func:`detect_split` proposes a ratio from
       :data:`PLAUSIBLE_SPLIT_RATIOS` and volume/transaction behavior
       corroborates it. Uncorroborated split-shaped moves stay unresolved.
    """
    window = ticker_bars[(ticker_bars["date"] > entry) & (ticker_bars["date"] <= exit_)]
    window = pd.concat([ticker_bars[ticker_bars["date"] == entry], window]).sort_values("date")
    recorded, implausible = foundry_splits_in_window(
        foundry_splits, ticker, cik, entry=entry, exit_=exit_
    )
    if implausible:
        # The table asserts an event and hands over a number no return may be
        # multiplied by. That is missing information, not a factor of 1.0.
        return 1.0, "none", True
    if len(window) < 2:
        # No pair to place or corroborate a recorded split against, while the
        # exit price will come from the delisting chain on a LATER date: an
        # unadjusted split would ride straight into the return.
        return (1.0, "none", True) if recorded else (1.0, "none", False)
    factor = 1.0
    sources: set[str] = set()
    rows = window.to_dict("records")
    placed: set[dt.date] = set()
    for prev, current in pairwise(rows):
        previous_day, day = prev["date"], current["date"]
        prev_close, close = float(prev["close"]), float(current["close"])
        # Every recorded split whose effective date falls in this gap, so a
        # session missing from the archive cannot hide one.
        in_gap = [
            (effective, ratio)
            for effective, ratio in recorded
            if previous_day < effective <= day
        ]
        if in_gap:
            placed.update(effective for effective, _ in in_gap)
            observed = prev_close / close if prev_close > 0 and close > 0 else None
            step = 1.0
            for _effective, ratio in in_gap:
                step *= ratio
            if observed is None:
                return 1.0, "none", True
            # The table's orientation convention is "factor = ratio", but a
            # confirmer that accepted 1/ratio has always been tolerated here;
            # arbitrate with the price rather than assume.
            best = min((step, 1.0 / step), key=lambda f: abs(observed / f - 1.0))
            if abs(observed / best - 1.0) > FOUNDRY_SPLIT_PRICE_TOLERANCE:
                # The authoritative table and the price series disagree. Either
                # could be right; neither can price this row honestly.
                return 1.0, "none", True
            factor *= best
            sources.add("foundry")
            continue
        candidate = detect_split(prev_close, close, tolerance)
        if candidate is None:
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
    # A recorded split after the window's last bar needs no adjustment: the exit
    # close this row will use predates it. One inside the bar range that no pair
    # claimed cannot be placed, and is unresolved.
    last_bar = rows[-1]["date"]
    if any(effective not in placed and effective <= last_bar for effective, _ in recorded):
        return 1.0, "none", True
    if not sources:
        return 1.0, "none", False
    source = sources.pop() if len(sources) == 1 else "mixed"
    return factor, source, False


# -- the build -----------------------------------------------------------------


def resolve_exit_price(
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
      Dividends are cash to a holder, never delisting proceeds; this
      attestation is computed exactly as it was before total returns existed.
    - ``return_is_total`` — True only when the vault carries the per-ex-date
      dividend archive AND the measured per-row coverage is >=
      :data:`TOTAL_RETURN_COVERAGE_BAR` (module docstring has the window and
      coverage semantics). The measured coverage is recorded in the sidecar
      whichever way the attestation lands.

    A frozen row missing its CIK is resolved through the foundry map AS OF
    its signal date (``universe(asof=...)`` point-in-time lookup) so a reused
    ticker never inherits its symbol's new owner; the sidecar's ``cik_map_modes``
    records the map that served each date, and falling back to the current
    snapshot changes no attestation — it is recorded, not overclaimed.
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

    # Every part consumed is verified against the sibling manifest first: a
    # frozen panel whose bytes the catalog does not vouch for must not become
    # forward evidence. Absent manifest (pre-convention directory) warns
    # inside the verifier and the read proceeds; a mismatch is a refusal.
    from .frozen_manifest import verify_sibling_manifest

    try:
        # The return value is load-bearing, not decorative: False means "no
        # manifest at all", which the verifier only warns about. Recording it
        # is what stops an unattested read from being indistinguishable from an
        # attested one in the sidecar and in the ledger's leakage_controls.
        # A list, not a generator: `all` short-circuits, and a later part's
        # sha256 mismatch must still raise even when an earlier one was
        # unmanifested.
        result.frozen_inputs_attested = all(
            [verify_sibling_manifest(panels[signal]) for signal in kept]
        )
    except ValueError as exc:
        result.refusal = str(exc)
        return result

    # One pass over the frozen panels to learn tickers and resolve CIKs. The
    # foundry CIK fallback is resolved PER SIGNAL DATE: tickers get reused
    # after delistings, so a missing CIK resolved through today's snapshot
    # could silently attach a dead issuer's row to its symbol's new owner.
    # ``universe(asof=signal_date)`` slices the foundry's published
    # point-in-time interval table at the signal date; when the archive cannot
    # reach it (or the provider predates ``asof``) the current snapshot is the
    # honest best available, and the sidecar's ``cik_map_modes`` records which
    # map served each date.
    frozen_frames = {signal: pd.read_parquet(panels[signal]) for signal in kept}

    def _universe_cik_map(asof: str | None) -> dict[str, str] | None:
        if foundry is None:
            return None
        try:
            records = foundry.universe(listed_only=False, asof=asof)
        except Exception:
            return None
        mapping: dict[str, str] = {}
        for record in records:
            ticker = str(record.get("ticker", "")).upper()
            cik = record.get("cik")
            if ticker and cik is not None and str(cik).strip().isdecimal():
                mapping.setdefault(ticker, str(cik).strip().zfill(10))
        return mapping

    snapshot_universe: dict[str, str] | None = None
    foundry_universe_by_date: dict[dt.date, dict[str, str]] = {}
    for signal in kept:
        replayed = _universe_cik_map(signal.isoformat())
        if replayed is not None:
            foundry_universe_by_date[signal] = replayed
            result.cik_map_modes[signal.isoformat()] = "pit_replay"
        else:
            if snapshot_universe is None:
                snapshot_universe = _universe_cik_map(None) or {}
            foundry_universe_by_date[signal] = snapshot_universe
            result.cik_map_modes[signal.isoformat()] = (
                "current_snapshot" if snapshot_universe else "unavailable"
            )
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

    dividend_events, dividend_months = load_dividend_events(
        vault, min(needed_days), max(needed_days), wanted
    )
    result.dividend_archive_months = len(dividend_months)

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
                cik = foundry_universe_by_date.get(signal, {}).get(ticker, "")
                if cik:
                    accounting.cik_from_foundry += 1
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

            resolution = resolve_exit_price(ticker_bars, vault, ticker, entry, exit_)
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

            # Dividend cash for the window (entry, exit_] — see the module
            # docstring for the boundary convention and the coverage rules.
            dividend_cash = 0.0
            dividend_count = 0
            dividend_covered = False
            if dividend_months and window_months(entry, exit_) <= dividend_months:
                in_window = [
                    (ex_date, cash, currency)
                    for ex_date, cash, currency in dividend_events.get(ticker, ())
                    if entry < ex_date <= exit_
                ]
                foreign_cash = any(currency not in ("", "USD") for _, _, currency in in_window)
                if foreign_cash:
                    pass  # non-USD cash against USD closes: basis unresolvable
                elif in_window and split_source != "none":
                    # "A split event occurred in (entry, exit]", not "the factor
                    # moved" — see signal_panel.py for the same correction.
                    pass  # mid-window split: per-ex-date share basis unknowable
                else:
                    dividend_covered = True
                    dividend_cash = sum(cash for _, cash, _ in in_window)
                    dividend_count = len(in_window)
            if dividend_covered:
                accounting.dividend_covered += 1
                if dividend_count:
                    accounting.dividend_cash_rows += 1
            else:
                accounting.dividend_uncovered += 1

            forward_return = (end_close * factor + dividend_cash) / start_close - 1.0
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
                    "dividend_cash": dividend_cash,
                    "dividend_count": dividend_count,
                    "dividend_covered": dividend_covered,
                    "terminal_price_used": terminal,
                    "symbol_changed": price_symbol.upper() != entry_symbol.upper(),
                    # Additive: the return window's declared length in sessions,
                    # so a backtest can observe the horizon as part of the
                    # panel's pre-registration spec instead of trusting a
                    # sidecar it never reads.
                    "horizon_days": config.horizon_days,
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

    kept_total = sum(p.kept for p in result.periods)
    covered_total = sum(p.dividend_covered for p in result.periods)
    result.dividend_coverage = covered_total / kept_total if kept_total else 0.0

    universe_is_pit = (outcome_dependent_drops == 0) and (min(panels) >= FORWARD_EPOCH)
    result.attestations = {
        "universe_is_pit": universe_is_pit,
        "return_is_total": bool(
            dividend_months
            and kept_total > 0
            and result.dividend_coverage >= TOTAL_RETURN_COVERAGE_BAR
        ),
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

    result.ready_for_backtest = (
        result.qualifying_periods >= config.min_periods and result.frozen_inputs_attested
    )
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
        "cik_map_modes": result.cik_map_modes,
        "unresolved_rows": result.unresolved_rows,
        "unresolved_fraction": result.unresolved_fraction,
        "dividend_coverage": result.dividend_coverage,
        "dividend_archive_months": result.dividend_archive_months,
        "frozen_inputs_attested": result.frozen_inputs_attested,
        "ready_for_backtest": result.ready_for_backtest,
    }


def write_panel(
    result: PanelBuildResult, out_dir: Path, profile: str, config: PanelBuildConfig
) -> tuple[Path | None, Path]:
    """Write ``<profile>.parquet`` (stable name — the ledger's experiment key is
    derived from the panel filename) and ``<profile>.build.json``, then refresh
    the sibling ``manifest.json`` so the ``backtest`` consumption of the panel
    verifies instead of trusts a bare path."""
    from .frozen_manifest import refresh_built_panel_manifest

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = out_dir / f"{profile}.build.json"
    sidecar_path.write_text(
        json.dumps(sidecar_payload(result, profile, config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if result.panel is None:
        refresh_built_panel_manifest(out_dir, built_now=frozenset({sidecar_path.name}))
        return None, sidecar_path
    panel_path = out_dir / f"{profile}.parquet"
    result.panel.to_parquet(panel_path, index=False)
    refresh_built_panel_manifest(
        out_dir, built_now=frozenset({panel_path.name, sidecar_path.name})
    )
    return panel_path, sidecar_path


def write_vault_manifest(
    directory: Path,
    license_note: str,
    source_urls: list[str],
    extra: dict[str, Any] | None = None,
) -> None:
    """Vault-shaped manifest over every non-dot, non-manifest file.

    A ~25-line copy rather than an import of ``stock_vault.manifest`` — the
    ecosystem rule is artifacts-not-imports, and this is the same precedent set
    for ``ticker_variants``. The shape must match exactly or
    ``VaultDataSource._manifest`` refuses the dataset. ``extra`` mirrors
    ``stock_vault.manifest.write_manifest``'s own escape hatch: additive
    dataset-specific keys beside the five contract keys, never replacing one.
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
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_urls": sorted(source_urls),
        "license_note": license_note,
        "files": files,
    }
    if extra:
        payload.update(extra)
        payload["schema_version"] = "1.0"
    (directory / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


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
    write_vault_manifest(
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
