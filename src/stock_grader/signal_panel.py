"""The return join for Stock-Vault's raw signal observations — the second half.

Stock-Vault exports what was OBSERVABLE about a name on its signal date: the
raw value, the pre-registered ``score``, the entry-side cross-section, and the
outcome WINDOW to measure over. It exports no returns. This module supplies
them, through the same chain :mod:`stock_grader.panel` already uses for frozen
score panels — :func:`~stock_grader.panel.split_factor`,
:func:`~stock_grader.panel.resolve_exit_price`, the per-ex-date dividend
window — so forward-return semantics have exactly one implementation in the
ecosystem.

**Why this module exists.** The computation lived twice: here, and in
``stock_vault/prices.py`` under a docstring that said "ported from
``stock_grader.panel._resolve_exit_price``". The copies drifted. The grader's
plausible-split table carries 1.5 and 2.5; the vault's never did, so a 3:2
split (price x 2/3) matched no ratio there, tripped no guard, and survived into
the panel as a fabricated ~-33% forward return — while the grader had already
moved to dividend-inclusive total return. Rule 1 forbids the cross-repo import
that would have deduplicated them, so the computation moved instead of the
code. ``tests/test_signal_panel.py`` plants that exact 3:2 split and proves the
fabricated return is gone.

**Licensing (ECOSYSTEM.md rule 5).** The joined panel embeds per-row returns
derived from Massive free-tier closes and stockanalysis.com delisted histories,
on top of FINRA/IB/Finnhub/SSGA-derived signals. Stock-Grader is PUBLIC, so
this builder writes ONLY into a private Stock-Vault clone — a path guard
enforces it and raises otherwise. Nothing it produces belongs in this
repository, not even as a number in a docstring.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .costs import COST_MODEL_ID, CostInputs, estimate_cost, golden_vector_sha256
from .panel import (
    TOTAL_RETURN_COVERAGE_BAR,
    load_bars,
    load_dividend_events,
    resolve_exit_price,
    split_factor,
    variant_map,
    window_months,
    write_vault_manifest,
)

__all__ = [
    "COST_INPUT_COLUMNS",
    "DEFAULT_POSITION_NOTIONAL_USD",
    "SIGNAL_PANEL_SCHEMA_VERSION",
    "SIGNAL_PANEL_VERSION",
    "SignalPanelConfig",
    "SignalPanelError",
    "SignalPanelResult",
    "SignalPeriodAccounting",
    "band_report",
    "band_verdict",
    "build_signal_panel",
    "evaluable_periods_from_counts",
    "is_band_namespace",
    "write_signal_panel",
]

#: A banded observation dataset is exported under its own namespace —
#: ``<signal>__adv<BAND ID>`` — so a band panel is identifiable from its name
#: alone, before any sidecar is opened. That matters because the §1.3 verdict
#: lives in a sidecar: if the sidecar is missing the block, the NAME is the only
#: remaining evidence that a verdict was owed, and something has to notice.
BAND_NAMESPACE = re.compile(r"__adv[A-Z][A-Za-z0-9]*$")


def is_band_namespace(signal: str) -> bool:
    """True for a signal name that declares itself an ADV band panel.

    The predicate exists so that an ABSENT ``adv_band`` block can be told apart
    from a panel that was never banded. Both look identical in the artifacts —
    no key — and only the namespace distinguishes "not a band" from "a band
    whose verdict went missing". Without it a stale or pre-gate sidecar is a
    silent pass, which is the exact inversion of "missing evidence is never a
    passing default".
    """
    return bool(BAND_NAMESPACE.search(str(signal)))


#: Row schema of the EVALUABLE panel this module writes.
SIGNAL_PANEL_SCHEMA_VERSION = "1.0"

#: The vault panel layout this builder consumes and writes into. It is a
#: constant, not a knob: v6 is the first layout where the vault exports
#: observations instead of returns, and reading a v5 layout with this chain
#: would silently mix two return semantics in one directory.
SIGNAL_PANEL_VERSION = 6

#: Columns a v6 observation part must carry. A part missing one of these is a
#: contract violation, not a row to be patched around.
REQUIRED_OBSERVATION_COLUMNS = frozenset(
    {
        "signal_date",
        "return_start",
        "return_end",
        "ticker",
        "score",
        "signal_raw",
        "membership_is_pit",
    }
)

#: Liquidity state the vault's observation part must carry for this join to
#: price a row's trading cost. All five or none: a cost assembled from four of
#: them and one fabricated default is worse than no cost at all, because it
#: reads exactly like a measured one. A panel whose observations omit these
#: carries NO cost column, attests ``per_row_costs_present: False``, and
#: evaluates on the flat charge exactly as it always did.
#:
#: * ``adv20_dollar`` — median ``close x volume`` over the trailing 20 archived
#:   sessions as of the signal date. Median, and dollars, and a window, so a
#:   one-day volume spike cannot reprice a name.
#: * ``cs21_spread_bps`` — Corwin-Schultz full spread in bps over the same
#:   21-session window, window mean floored at zero ONCE, tick floor NOT yet
#:   applied (:func:`stock_grader.costs.spread_bps` owns that).
#: * ``sigma21`` — sd of daily log close-to-close returns over that window,
#:   split-cleaned.
#: * ``amihud_lambda_bps_per_musd`` — median daily ``|r| / $volume`` over that
#:   window, in bps of price per $1M traded.
#: * ``cost_usable_pairs`` — consecutive-session pairs that survived the split
#:   exclusion, so this side can apply the short-window refusal rather than
#:   trusting that the producer did.
COST_INPUT_COLUMNS = (
    "adv20_dollar",
    "cs21_spread_bps",
    "sigma21",
    "amihud_lambda_bps_per_musd",
    "cost_usable_pairs",
)

#: Per-position notional the emitted ``round_trip_cost_bps`` is priced at.
#: Cost is a function of size, so a cost column without a declared size is
#: meaningless; $100k is one position of a $1M book spread across ten names,
#: the size at which the participation cap starts to bind in thin names rather
#: than a frictionless toy. The panel carries the raw inputs alongside, so any
#: other rung of the capacity ladder is a recomputation, not a rebuild.
DEFAULT_POSITION_NOTIONAL_USD = 100_000.0


class SignalPanelError(RuntimeError):
    """The join cannot proceed honestly (bad artifact, escaped path)."""


@dataclass(frozen=True, slots=True)
class SignalPanelConfig:
    split_tolerance: float = 0.01
    rebuild: bool = False
    cost_position_notional_usd: float = DEFAULT_POSITION_NOTIONAL_USD


@dataclass(slots=True)
class SignalPeriodAccounting:
    """Every observation of a signal date accounted for.

    ``observations == kept + no_start_price_dropped + unresolved_dropped``
    always holds; the ``resolved_*`` and ``split_adjusted_*`` counters say
    which chain tier served each kept row.
    """

    signal_date: str
    return_start: str
    return_end: str
    observations: int = 0
    no_start_price_dropped: int = 0
    unresolved_dropped: int = 0
    resolved_market_eod: int = 0
    resolved_delisted_archive: int = 0
    resolved_last_listed_close: int = 0
    split_adjusted_foundry: int = 0
    split_adjusted_reconstructed: int = 0
    dividend_covered: int = 0
    dividend_uncovered: int = 0
    dividend_cash_rows: int = 0
    pit_membership_rows: int = 0
    # Cost accounting, all three counted rather than inferred. ``cost_priced``
    # plus ``no_cost_estimate`` equals ``kept`` whenever the observation part
    # carried the inputs at all; when it did not, both are 0 and the panel-level
    # attestation says so, which is a different fact from "every row refused".
    cost_priced: int = 0
    no_cost_estimate: int = 0
    capacity_truncated_rows: int = 0
    # Notional the participation cap refused, as a fraction of notional
    # requested, over this signal date. The headline capacity number: a band
    # whose orders are mostly refused has no edge to measure at that size,
    # whatever its cost column says about the fraction that did fit.
    capacity_truncated_notional_fraction: float = 0.0
    # WHAT priced this date, persisted with the counts. An incremental run
    # re-prices nothing, so without these the whole-panel block would report a
    # null cost model for a panel whose every row carries one — the same
    # last-run-only failure the unresolved-ticker field was fixed for. They also
    # make a mixed-size panel detectable: two signal dates priced at two
    # notionals are two measurements sharing one column name.
    cost_model_id: str | None = None
    cost_position_notional_usd: float | None = None
    kept: int = 0
    # The tickers behind ``unresolved_dropped``, persisted per signal date.
    # Without this field the identities lived only in the run that priced them:
    # build.json reported them in its whole-panel block while an incremental
    # run — which re-prices nothing — rebuilt that block from counts.json and
    # emitted an empty list beside a non-zero unresolved_rows. An affirmative
    # "no repeat offenders" is exactly the optimistic reading the accounting
    # exists to prevent.
    unresolved_tickers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SignalPanelResult:
    signal: str
    panel_version: int = SIGNAL_PANEL_VERSION
    periods: list[SignalPeriodAccounting] = field(default_factory=list)
    parts_written: int = 0
    rows_written: int = 0
    observation_periods: int = 0
    observations: int = 0
    kept_rows: int = 0
    unresolved_rows: int = 0
    unresolved_fraction: float = 0.0
    unresolved_tickers: list[str] = field(default_factory=list)
    # Signal dates whose persisted accounting records unresolved drops but no
    # offender identities: parts closed before SignalPeriodAccounting carried
    # the field. Named rather than papered over — an empty union over these
    # dates is missing data, not an absence of offenders.
    unresolved_tickers_incomplete_dates: list[str] = field(default_factory=list)
    # Whole-panel coverage, from the SAME persisted accounting as everything
    # else here. A signal date can lose 100% of its observations (no archived
    # entry bar) and simply vanish from the rollup, contributing 0 to every
    # numerator AND every denominator; that is a pre-registration denominator
    # problem, and it needs a number of its own rather than a silently perfect
    # attestation.
    panel_observations: int = 0
    no_start_price_rows: int = 0
    survival_rate: float = 0.0
    # Whole-panel cost accounting, from the same persisted per-date blocks as
    # everything else here.
    cost_priced_rows: int = 0
    no_cost_estimate_rows: int = 0
    cost_coverage: float = 0.0
    capacity_truncated_rows: int = 0
    capacity_truncated_notional_fraction: float = 0.0
    cost_position_notional_usd: float | None = None
    cost_model_id: str | None = None
    # Signal dates whose closed accounting names a different cost model or a
    # different position size from the rest of the panel. Non-empty means the
    # cost column mixes two measurements and the panel refuses rather than
    # publishing a mean over both.
    cost_inconsistent_dates: list[str] = field(default_factory=list)
    periods_accounted: int = 0
    periods_in_panel: int = 0
    dividend_coverage: float = 0.0
    dividend_archive_months: int = 0
    pit_membership_coverage: float = 0.0
    attestations: dict[str, bool] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    #: The observation panel's ``adv_band`` block, VERBATIM from Stock-Vault's
    #: manifest, or ``None`` for an unbanded panel. It carries the band's
    #: identity (id, dollar edges, label, control flag), the pre-registration
    #: it was cut under, and the vault's own §1.3 verdict — including the
    #: ``reportable`` flag that had no consumer on this side of the wall until
    #: :func:`band_report` gave it one.
    adv_band: dict[str, Any] | None = None
    refusal: str | None = None


# -- reading the vault's observation artifact ---------------------------------


def _check_observation_columns(signal: str, day: dt.date, frame: pd.DataFrame) -> None:
    missing = sorted(REQUIRED_OBSERVATION_COLUMNS - set(frame.columns))
    if missing:
        raise SignalPanelError(
            f"{signal} observation part {day} is missing required column(s): "
            + ", ".join(missing)
            + ". A v5-or-earlier layout carries its own forward_return and must not "
            "be re-joined by this chain."
        )
    if "forward_return" in frame.columns:
        raise SignalPanelError(
            f"{signal} observation part {day} already carries forward_return: the "
            "vault must export observations, not returns, or the single-owner "
            "guarantee is void."
        )


def _finite(value: Any) -> float | None:
    """A float, or None for anything that is not one. Never a substituted zero."""

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cost_inputs(observation: Any, price: float) -> CostInputs | None:
    """Read one row's liquidity state off the observation, or refuse.

    ``price`` is the ENTRY close, not the signal-date close: it is the price the
    fill happens at, so the tick floor is measured against the price actually
    paid. The estimation window still ends at the signal date — the producer
    owns it, and it must, because only the producer can see the trailing bars.
    That one-session offset is the price of the v6 split and is disclosed rather
    than papered over.
    """

    adv = _finite(getattr(observation, "adv20_dollar", None))
    spread = _finite(getattr(observation, "cs21_spread_bps", None))
    sigma = _finite(getattr(observation, "sigma21", None))
    lam = _finite(getattr(observation, "amihud_lambda_bps_per_musd", None))
    pairs = _finite(getattr(observation, "cost_usable_pairs", None))
    if adv is None or spread is None or sigma is None or lam is None or pairs is None:
        return None
    inputs = CostInputs(
        price=price,
        adv20_dollar=adv,
        sigma=sigma,
        cs_spread_bps=spread,
        amihud_lambda_bps_per_musd=lam,
        usable_pairs=int(pairs),
    )
    return inputs if inputs.is_estimable() else None


def _panel_dir(vault: Any, signal: str, version: int) -> Path:
    """``<vault>/data/signal_panels/<signal>/v<version>``, path-guarded.

    Rule 5's structural guarantee: this builder can only ever write inside a
    private vault clone. An out_dir that escapes the vault root is a bug that
    would put licensed per-row returns somewhere public, so it raises.
    """
    root = Path(vault.root).resolve()
    out_dir = (root / "data" / "signal_panels" / signal / f"v{version}").resolve()
    if root not in out_dir.parents:
        raise SignalPanelError(
            f"signal panel path {out_dir} escapes the vault {root}; refusing to write "
            "licensed per-row returns outside the private archive"
        )
    return out_dir


# -- the join ------------------------------------------------------------------


def build_signal_panel(
    vault: Any,
    signal: str,
    *,
    foundry: Any = None,
    version: int = SIGNAL_PANEL_VERSION,
    config: SignalPanelConfig | None = None,
    log=lambda _message: None,
) -> tuple[SignalPanelResult, dict[dt.date, pd.DataFrame]]:
    """Join one signal's raw observations to realized forward returns.

    Returns ``(result, new_parts)``: the accounting plus the per-signal-date
    frames this call produced. Signal dates whose part already exists are NOT
    re-priced — per-date parts are immutable, and their accounting is restored
    from the persisted ``counts.json`` instead of recomputed, so a monthly
    incremental run and a full rebuild report the same whole-panel numbers.

    Attestations are computed, never declared:

    - ``universe_is_pit`` — True only when every kept row's membership came
      from the foundry's published point-in-time interval tables AND zero rows
      were dropped for an outcome-dependent reason. Partial coverage keeps it
      False and reports ``pit_membership_coverage`` instead of rounding up.
    - ``return_is_total`` — True only when the vault carries the per-ex-date
      dividend archive and measured per-row coverage reaches
      :data:`~stock_grader.panel.TOTAL_RETURN_COVERAGE_BAR`.
    - ``delisting_return_included`` — True only when no row was unresolved
      (and the panel is non-empty). A last listed close is a disclosed
      truncation convention, not delisting proceeds; the counters record how
      many rows leaned on it.
    """
    config = config or SignalPanelConfig()
    result = SignalPanelResult(signal=signal, panel_version=version)

    manifest = vault.signal_panel_manifest(signal, version)
    result.spec = {
        key: manifest.get(key)
        for key in (
            "signal",
            "definition",
            "direction",
            "periods_per_year",
            "horizon",
            "overlapping_windows",
            "panel_version",
            "observation_schema_version",
        )
    }
    # The ADV band this observation dataset was cut for, if any. Read here with
    # the rest of the manifest so an incremental run that prices nothing still
    # republishes it — the band verdict is a property of the panel, not of the
    # run, exactly like the attestations beside it.
    band = manifest.get("adv_band")
    result.adv_band = dict(band) if isinstance(band, dict) and band else None
    # Fail CLOSED on the one case the presence check cannot cover. Everywhere
    # downstream, an absent block means "not a band" and evaluates unqualified;
    # that reading is only safe while an absent block cannot happen to a band.
    # It can: the eight banded panels of the small-cap program were built before
    # this verdict existed, and their sidecars carry no block at all. Building a
    # band panel from a manifest that declares no band would mint another one.
    if result.adv_band is None and is_band_namespace(signal):
        raise SignalPanelError(
            f"{signal} is a banded namespace but its observation manifest carries no "
            "adv_band block, so no §1.3 evaluable-period verdict can be computed for "
            "it. A panel written from here would be indistinguishable from an "
            "unbanded one and would evaluate with no floor at all — a band below the "
            "pre-registered floor would be reported as a band result. Re-export the "
            "observation dataset from Stock-Vault with its adv_band block, or build "
            "this panel under a namespace that does not claim to be a band."
        )
    observations = vault.signal_panel_observations(signal, version)
    result.observation_periods = len(observations)
    result.observations = int(sum(len(frame) for frame in observations.values()))
    for day, frame in observations.items():
        _check_observation_columns(signal, day, frame)
    # Read from the archive, not from this run's work: an incremental run that
    # prices nothing must still report the same return_is_total the last full
    # build did, or the attestation would flicker with the schedule.
    months_of = getattr(vault, "dividend_months", None)
    result.dividend_archive_months = len(months_of()) if months_of is not None else 0
    if not observations:
        return result, {}

    out_dir = _panel_dir(vault, signal, version)
    pending = sorted(
        day
        for day in observations
        if config.rebuild or not (out_dir / f"{day.isoformat()}.parquet").is_file()
    )
    if not pending:
        log(f"{signal}: every part already built; rollup only")
        return result, {}

    # One bulk load for every window this run must price. Windows come from the
    # observation rows: the vault owns the signal's cadence, so the return join
    # never re-derives a horizon it might get wrong.
    windows: dict[dt.date, tuple[dt.date, dt.date]] = {}
    for day in pending:
        frame = observations[day]
        starts = set(frame["return_start"].astype(str))
        ends = set(frame["return_end"].astype(str))
        if len(starts) != 1 or len(ends) != 1:
            raise SignalPanelError(
                f"{signal} observation part {day} mixes return windows "
                f"({sorted(starts)} -> {sorted(ends)}); the evaluator requires one "
                "window per signal date"
            )
        entry = dt.date.fromisoformat(next(iter(starts)))
        exit_ = dt.date.fromisoformat(next(iter(ends)))
        if exit_ <= entry:
            # A degenerate window measures one close against itself: every
            # forward return is exactly 0.0 and the whole cross-section is
            # noise-free nonsense. The producer refuses to emit these, and the
            # single owner of return semantics refuses to price one — an
            # honest refusal beats a panel of zeros that attests perfectly.
            raise SignalPanelError(
                f"{signal} observation part {day} has a zero-length return window "
                f"({entry} -> {exit_}): entry close IS exit close, so no forward "
                "return exists to join"
            )
        windows[day] = (entry, exit_)

    days = sorted(vault.market_eod_available_days())
    available = set(days)
    # Entry pricing requires an EXACT bar on the entry day, so a signal date
    # whose entry day is absent from this clone loses 100% of its observations,
    # writes no part, and disappears from panel.parquet — contributing nothing
    # to any numerator OR denominator while the survivors attest perfectly.
    # The only structural guard used to be "no pending window has a single
    # archived day", which nine healthy windows satisfy on behalf of three
    # missing ones. Refuse per period instead: an honest refusal beats a
    # silently truncated panel (the same principle as the zero-length window
    # refusal above).
    uncovered = sorted(
        f"{day.isoformat()} (entry {entry.isoformat()})"
        for day, (entry, _exit) in windows.items()
        if entry not in available
    )
    if uncovered:
        result.refusal = (
            "the vault EOD archive has no bar on the entry day of "
            f"{len(uncovered)} pending signal date(s): {', '.join(uncovered)}. "
            "Every observation of those dates would drop for want of an entry "
            "price and the period would vanish from the panel entirely; refusing "
            "rather than silently shrinking the pre-registered trial count. A "
            "shallow or partially synced vault clone, or an archive window that "
            "has rolled past these dates, is the usual cause."
        )
        return result, {}
    needed_days: set[dt.date] = set()
    for entry, exit_ in windows.values():
        needed_days.update(d for d in days if entry <= d <= exit_)
    if not needed_days:
        result.refusal = "the vault EOD archive covers none of the pending return windows"
        return result, {}

    tickers: set[str] = set()
    for day in pending:
        tickers.update(str(t).upper() for t in observations[day]["ticker"].tolist())
    wanted = variant_map(tickers)
    bars = load_bars(vault, needed_days, wanted)

    dividend_events, dividend_months = load_dividend_events(
        vault, min(needed_days), max(needed_days), wanted
    )

    foundry_splits = None
    if foundry is not None:
        try:
            foundry_splits = foundry.splits()
        except Exception:
            # No foundry splits: the detector degrades to its volume/transaction
            # corroboration tier, exactly as build_panel does. Never fatal.
            foundry_splits = None

    from .research_manifest import package_commit

    # NOT current_commit(): this builder runs with the Stock-VAULT checkout as
    # the process working directory (the vault's signal-panels workflow owns
    # the job), so a bare `git rev-parse` stamps a vault hash into a column
    # whose whole purpose is to name the revision of the return-join
    # implementation. Rows would then carry a hash that resolves in neither
    # tree, and two parts priced by two different return implementations would
    # be indistinguishable after the fact.
    builder_commit = package_commit()
    # All five inputs on every pending part, or none of them anywhere. A panel
    # whose early parts carry costs and whose later parts do not would evaluate
    # half its periods net and half gross under one heading, and the mean would
    # be a number describing nothing.
    costs_available = all(
        set(COST_INPUT_COLUMNS) <= set(observations[day].columns) for day in pending
    )
    if costs_available:
        log(
            f"{signal}: pricing per-row costs at "
            f"${config.cost_position_notional_usd:,.0f} per position ({COST_MODEL_ID})"
        )
    else:
        missing_inputs = sorted(
            set(COST_INPUT_COLUMNS)
            - set.intersection(*(set(observations[day].columns) for day in pending))
        )
        log(
            f"{signal}: no per-row cost column — the observation parts do not carry "
            + ", ".join(missing_inputs)
            + ". The panel will evaluate on the evaluator's flat charge and attest "
            "per_row_costs_present: false."
        )
    new_parts: dict[dt.date, pd.DataFrame] = {}

    for signal_date in pending:
        entry, exit_ = windows[signal_date]
        frame = observations[signal_date]
        accounting = SignalPeriodAccounting(
            signal_date=signal_date.isoformat(),
            return_start=entry.isoformat(),
            return_end=exit_.isoformat(),
            observations=len(frame),
            cost_model_id=COST_MODEL_ID if costs_available else None,
            cost_position_notional_usd=(
                float(config.cost_position_notional_usd) if costs_available else None
            ),
        )
        # One slice per signal date, then group: every ticker in this
        # cross-section shares the window, so the per-row work is a dict lookup
        # instead of a fresh boolean mask over a multi-year bar frame.
        window_bars = bars[(bars["date"] >= entry) & (bars["date"] <= exit_)]
        by_ticker = dict(tuple(window_bars.groupby("ticker"))) if len(window_bars) else {}

        rows: list[dict[str, Any]] = []
        for observation in frame.itertuples(index=False):
            ticker = str(observation.ticker).upper()
            ticker_bars = by_ticker.get(ticker)
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

            cik_value = getattr(observation, "cik", None)
            cik = "" if cik_value is None or pd.isna(cik_value) else str(cik_value).strip()

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
                accounting.unresolved_tickers.append(ticker)
                continue

            resolution = resolve_exit_price(ticker_bars, vault, ticker, entry, exit_)
            if resolution is None:
                accounting.unresolved_dropped += 1
                accounting.unresolved_tickers.append(ticker)
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

            # Dividend cash for (entry, exit_] — the window and coverage rules
            # are panel.py's, verbatim, because they are the same semantics.
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
                    # moved". Keying on factor != 1.0 silently declared the
                    # basis safe for every split the price signature could not
                    # see, adding pre-split cash on a post-split share basis.
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

            membership_is_pit = bool(observation.membership_is_pit)
            if membership_is_pit:
                accounting.pit_membership_rows += 1

            # Cost is a property of the row, priced at the entry close the fill
            # uses. A row whose inputs cannot support an estimate gets NaN and
            # is counted — never a band average, never a back-filled constant,
            # because a fabricated cost is indistinguishable from a measured one
            # by the time anybody reads the panel.
            cost_columns: dict[str, Any] = {}
            if costs_available:
                inputs = _cost_inputs(observation, start_close)
                estimate = (
                    None
                    if inputs is None
                    else estimate_cost(inputs, config.cost_position_notional_usd)
                )
                # `inputs is None` already implies `estimate is None`, but spelling both
                # out is what lets the else-branch below read inputs.* unguarded.
                if inputs is None or estimate is None:
                    accounting.no_cost_estimate += 1
                    cost_columns = {
                        "round_trip_cost_bps": float("nan"),
                        "one_way_cost_bps": float("nan"),
                        "half_spread_bps": float("nan"),
                        "impact_bps": float("nan"),
                        "cost_participation": float("nan"),
                        "cost_capacity_truncated": False,
                        "cost_notional_target_usd": float(config.cost_position_notional_usd),
                        "cost_notional_allowed_usd": float("nan"),
                        "cost_adv20_dollar": float("nan"),
                        "cost_sigma": float("nan"),
                        "cost_cs_spread_bps": float("nan"),
                        "cost_amihud_lambda_bps_per_musd": float("nan"),
                        "cost_model_id": COST_MODEL_ID,
                    }
                else:
                    accounting.cost_priced += 1
                    if estimate.capacity_truncated:
                        accounting.capacity_truncated_rows += 1
                    cost_columns = {
                        "round_trip_cost_bps": estimate.round_trip_bps,
                        "one_way_cost_bps": estimate.one_way_bps,
                        "half_spread_bps": estimate.half_spread_bps,
                        "impact_bps": estimate.impact_bps,
                        "cost_participation": estimate.participation,
                        "cost_capacity_truncated": estimate.capacity_truncated,
                        "cost_notional_target_usd": estimate.notional_target_usd,
                        "cost_notional_allowed_usd": estimate.notional_allowed_usd,
                        # The raw inputs ride along so the capacity ladder is a
                        # recomputation over this panel rather than a rebuild of
                        # it: parts are immutable, and re-pricing four rungs by
                        # rebuilding would need four namespaces.
                        "cost_adv20_dollar": inputs.adv20_dollar,
                        "cost_sigma": inputs.sigma,
                        "cost_cs_spread_bps": inputs.cs_spread_bps,
                        "cost_amihud_lambda_bps_per_musd": (inputs.amihud_lambda_bps_per_musd),
                        "cost_model_id": COST_MODEL_ID,
                    }

            rows.append(
                {
                    "signal_date": signal_date.isoformat(),
                    "return_start": entry.isoformat(),
                    "return_end": exit_.isoformat(),
                    "ticker": str(observation.ticker),
                    "cik": cik or None,
                    "security_id": getattr(observation, "security_id", None),
                    # Carried through untouched: the sign was applied at
                    # observation time, before any return existed. Nothing in
                    # this module may move a percentile.
                    "score": float(observation.score),
                    "signal_raw": float(observation.signal_raw),
                    "forward_return": (end_close * factor + dividend_cash) / start_close - 1.0,
                    "filed_through": str(getattr(observation, "filed_through", "")),
                    "source_asof": str(getattr(observation, "source_asof", "")),
                    "signal_name": str(getattr(observation, "signal_name", signal)),
                    "signal_direction": int(getattr(observation, "signal_direction", 0)),
                    "membership_is_pit": membership_is_pit,
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
                    "overlapping_windows": bool(getattr(observation, "overlapping_windows", False)),
                    "horizon_trading_days": int(getattr(observation, "horizon_trading_days", 0)),
                    "vault_commit": str(getattr(observation, "vault_commit", "")),
                    "observation_schema_version": str(getattr(observation, "schema_version", "")),
                    "panel_version": version,
                    "panel_schema_version": SIGNAL_PANEL_SCHEMA_VERSION,
                    "builder_commit": builder_commit,
                    **cost_columns,
                }
            )
            accounting.kept += 1

        accounting.unresolved_tickers = sorted(set(accounting.unresolved_tickers))
        if costs_available and rows:
            target = sum(
                float(row["cost_notional_target_usd"])
                for row in rows
                if math.isfinite(float(row["cost_notional_allowed_usd"]))
            )
            allowed = sum(
                float(row["cost_notional_allowed_usd"])
                for row in rows
                if math.isfinite(float(row["cost_notional_allowed_usd"]))
            )
            accounting.capacity_truncated_notional_fraction = (
                0.0 if target <= 0.0 else max(0.0, 1.0 - allowed / target)
            )
        result.periods.append(accounting)
        if rows:
            part = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
            new_parts[signal_date] = part
        log(
            f"{signal} {signal_date}: kept={accounting.kept} "
            f"no_entry={accounting.no_start_price_dropped} "
            f"unresolved={accounting.unresolved_dropped}"
        )

    return result, new_parts


# -- persistence ---------------------------------------------------------------


def _aggregate(counts_by_date: dict[str, dict], result: SignalPanelResult) -> None:
    """Whole-panel numbers from the PERSISTED per-date accounting.

    Closed parts are immutable, so their accounting can never be recomputed —
    the same discipline the vault applies to its own counts.json. Reading the
    file back means an incremental run and a full rebuild report identical
    whole-panel attestations.

    Everything this function sets is whole-panel, including the unresolved
    ticker identities and the observation->kept survival numbers: a field in
    the whole-panel block that is actually last-run-only reads as an
    affirmative fact ("no repeat offenders") when it is really missing data.
    """
    kept = sum(int(entry.get("kept", 0)) for entry in counts_by_date.values())
    unresolved = sum(int(entry.get("unresolved_dropped", 0)) for entry in counts_by_date.values())
    covered = sum(int(entry.get("dividend_covered", 0)) for entry in counts_by_date.values())
    pit_rows = sum(int(entry.get("pit_membership_rows", 0)) for entry in counts_by_date.values())
    observed = sum(int(entry.get("observations", 0)) for entry in counts_by_date.values())
    no_start = sum(int(entry.get("no_start_price_dropped", 0)) for entry in counts_by_date.values())
    considered = kept + unresolved
    result.kept_rows = kept
    result.unresolved_rows = unresolved
    result.unresolved_fraction = unresolved / considered if considered else 0.0
    result.dividend_coverage = covered / kept if kept else 0.0
    result.pit_membership_coverage = pit_rows / kept if kept else 0.0
    result.panel_observations = observed
    result.no_start_price_rows = no_start
    result.survival_rate = kept / observed if observed else 0.0
    priced = sum(int(entry.get("cost_priced", 0)) for entry in counts_by_date.values())
    refused = sum(int(entry.get("no_cost_estimate", 0)) for entry in counts_by_date.values())
    result.cost_priced_rows = priced
    result.no_cost_estimate_rows = refused
    # Denominator is the rows that WERE offered to the cost model, not every
    # kept row: on a panel with no cost inputs at all both counters are zero,
    # and a coverage of 0/0 reported as 0.0 beside a False attestation says
    # "not attempted", where 0/kept would say "attempted and failed".
    costed = priced + refused
    result.cost_coverage = priced / costed if costed else 0.0
    result.capacity_truncated_rows = sum(
        int(entry.get("capacity_truncated_rows", 0)) for entry in counts_by_date.values()
    )
    truncation_weights = [
        (
            int(entry.get("cost_priced", 0)),
            float(entry.get("capacity_truncated_notional_fraction", 0.0)),
        )
        for entry in counts_by_date.values()
    ]
    weight = sum(rows for rows, _fraction in truncation_weights)
    result.capacity_truncated_notional_fraction = (
        sum(rows * fraction for rows, fraction in truncation_weights) / weight if weight else 0.0
    )
    # Restored from the persisted accounting, not from this run: an incremental
    # run re-prices nothing and must still name the cost model the closed parts
    # were priced under.
    models = {
        str(entry["cost_model_id"])
        for entry in counts_by_date.values()
        if entry.get("cost_model_id")
    }
    notionals = {
        float(entry["cost_position_notional_usd"])
        for entry in counts_by_date.values()
        if entry.get("cost_position_notional_usd") is not None
    }
    result.cost_model_id = models.pop() if len(models) == 1 else None
    result.cost_position_notional_usd = notionals.pop() if len(notionals) == 1 else None
    result.cost_inconsistent_dates = sorted(
        signal_date
        for signal_date, entry in counts_by_date.items()
        if int(entry.get("cost_priced", 0)) > 0
        and (
            result.cost_model_id is None
            or str(entry.get("cost_model_id") or "") != result.cost_model_id
            or result.cost_position_notional_usd is None
            or float(entry.get("cost_position_notional_usd") or 0.0)
            != result.cost_position_notional_usd
        )
    )
    result.periods_accounted = len(counts_by_date)
    result.periods_in_panel = evaluable_periods_from_counts(counts_by_date.values())
    # Offender identities are whole-panel too, or the label lies: they are
    # unioned from the SAME persisted accounting as every number beside them.
    identities: set[str] = set()
    incomplete: list[str] = []
    for signal_date, entry in counts_by_date.items():
        recorded = entry.get("unresolved_tickers")
        listed = [str(item) for item in recorded] if isinstance(recorded, list) else []
        identities.update(listed)
        if int(entry.get("unresolved_dropped", 0)) > 0 and not listed:
            incomplete.append(str(signal_date))
    result.unresolved_tickers = sorted(identities)
    result.unresolved_tickers_incomplete_dates = sorted(incomplete)
    result.attestations = {
        # Zero outcome-dependent drops AND full point-in-time membership. Both
        # legs, or the panel says False and reports its coverage.
        "universe_is_pit": bool(kept > 0 and unresolved == 0 and pit_rows == kept),
        "return_is_total": bool(
            result.dividend_archive_months
            and kept > 0
            and result.dividend_coverage >= TOTAL_RETURN_COVERAGE_BAR
        ),
        "delisting_return_included": bool(kept > 0 and unresolved == 0),
        # Computed, like the other three. True only when every kept row of
        # every accounted period carries a real cost estimate. One refused row
        # anywhere makes it False and leaves ``no_cost_estimate_rows`` to say
        # how many — the evaluator then knows the net numbers are missing names,
        # rather than believing a panel that quietly dropped its thinnest ones.
        "per_row_costs_present": bool(kept > 0 and costed == kept and refused == 0),
    }


def band_report(result: SignalPanelResult) -> dict[str, Any] | None:
    """The band block the EVALUABLE panel publishes, or ``None`` if unbanded.

    Stock-Vault computes a §1.3 verdict for every ADV band it exports and
    writes it onto the observation manifest. Nothing on this side read it: the
    return join copied the spec keys and dropped ``adv_band``, so the evaluable
    panel — the artifact the evaluator actually opens — carried no band
    identity and no floor at all. A band below the pre-registered
    30-evaluable-period floor was therefore indistinguishable from a band above
    it, and its statistics would have been reported as if the program permitted
    them.

    Two facts are published side by side rather than merged, because they are
    measurements of two different artifacts:

    * ``observations`` — the vault's block, VERBATIM. Its ``evaluable_periods``
      counts signal dates that cleared the 200-name floor and were exported.
    * ``evaluable_periods`` — periods of THIS panel that carry at least one
      priced row, from the persisted ``counts.json`` like every other
      whole-panel number here. It can only be smaller: the return join drops a
      period whose entry bars the archive does not have.

    ``reportable`` is the AND of both, so the binding constraint is whichever
    artifact is thinner, and ``not_reportable_because`` names the leg(s) that
    failed. A missing floor is not treated as a passing one — an observation
    manifest that declares no floor cannot license a report.
    """
    return band_verdict(result.adv_band, result.periods_in_panel)


def evaluable_periods_from_counts(entries: Any) -> int:
    """Periods of a built panel carrying at least one kept row, from ``counts.json``.

    The single definition of the §1.3 denominator. ``_aggregate`` calls it while
    a build is in flight; the evaluator calls it against a persisted
    ``counts.json`` when it has to recompose a verdict a stale ``build.json``
    never carried. A second inline ``sum(... kept > 0)`` anywhere would be a
    second definition of "evaluable", which is the number the whole gate turns
    on.
    """
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            kept = int(entry.get("kept", 0) or 0)
        except (TypeError, ValueError):
            continue
        if kept > 0:
            total += 1
    return total


def band_verdict(observed: dict[str, Any] | None, evaluable_periods: int) -> dict[str, Any] | None:
    """:func:`band_report`'s arithmetic, over the two inputs it actually reads.

    Split out because the verdict has a second reader. ``band_report`` composes
    it at BUILD time from a live ``SignalPanelResult``; the evaluator has to be
    able to recompose it at EVALUATE time from a panel directory whose
    ``build.json`` predates the verdict, and the one thing it must not do is
    reimplement the AND. One function, two callers, no second opinion.
    """
    if not isinstance(observed, dict) or not observed:
        return None
    raw_band = observed.get("band")
    band: dict[str, Any] = dict(raw_band) if isinstance(raw_band, dict) else {}
    floor_raw = observed.get("min_evaluable_periods")
    try:
        floor = None if floor_raw is None else int(floor_raw)
    except (TypeError, ValueError):
        floor = None
    evaluable = int(evaluable_periods)
    upstream_periods = observed.get("evaluable_periods")
    upstream_reportable = bool(observed.get("reportable", False))

    reasons: list[str] = []
    if floor is None:
        reasons.append(
            "the observation manifest declares no min_evaluable_periods floor, so "
            "nothing here can say whether this band clears the pre-registered one"
        )
    else:
        if not upstream_reportable:
            reasons.append(
                f"the observation panel is NOT REPORTABLE: {upstream_periods} "
                f"evaluable period(s) against a floor of {floor}"
            )
        if evaluable < floor:
            reasons.append(
                f"the evaluable panel has {evaluable} period(s) with at least one "
                f"priced row, against a floor of {floor}"
            )
    return {
        "band_id": band.get("band_id"),
        "band": band,
        "min_evaluable_periods": floor,
        "evaluable_periods": evaluable,
        "reportable": not reasons,
        "not_reportable_because": reasons,
        "preregistration": observed.get("preregistration"),
        "preregistration_sha256": observed.get("preregistration_sha256"),
        # The producer's own block, unedited. Copied rather than re-derived:
        # only the vault can see the trailing bars its screen ran on, and a
        # re-derivation here would be a second, silently divergent measurement.
        "observations": observed,
        "observations_source": "Stock-Vault observation manifest (adv_band)",
    }


def write_signal_panel(
    vault: Any,
    signal: str,
    result: SignalPanelResult,
    new_parts: dict[dt.date, pd.DataFrame],
    *,
    version: int = SIGNAL_PANEL_VERSION,
    license_note: str | None = None,
    source_urls: list[str] | None = None,
) -> Path:
    """Write the evaluable panel into the vault beside its observations.

    Layout (``data/signal_panels/<signal>/v<version>/``)::

        observations/        raw parts + manifest, written by Stock-Vault
        <signal_date>.parquet  evaluable per-period part, immutable
        panel.parquet          rollup the evaluator reads
        counts.json            per-signal-date accounting, never rewritten
        build.json             attestations, coverage, this run
        manifest.json          vault-shaped catalog over the files above

    The three attestation columns live on the ROLLUP only. An attestation is a
    property of the whole panel, and per-date parts are immutable — writing a
    whole-panel verdict into a part would either freeze a stale answer or force
    a rewrite of closed history.
    """
    out_dir = _panel_dir(vault, signal, version)
    out_dir.mkdir(parents=True, exist_ok=True)

    counts_path = out_dir / "counts.json"
    counts_by_date: dict[str, dict] = (
        json.loads(counts_path.read_text(encoding="utf-8")) if counts_path.is_file() else {}
    )
    for accounting in result.periods:
        # A re-priced signal date that now yields ZERO rows writes no part, so
        # the stale part from the previous build survives and keeps
        # contributing its rows to the rollup while its accounting is
        # overwritten with zeros. Deleting the part would destroy closed
        # evidence; keeping both would attest over rows whose outcome-dependent
        # drops are no longer in the ledger. Refuse, and version instead.
        stale_part = out_dir / f"{accounting.signal_date}.parquet"
        if accounting.kept == 0 and stale_part.is_file():
            raise SignalPanelError(
                f"{signal} {accounting.signal_date} re-priced to zero kept rows but "
                f"{stale_part.name} is already on disk with "
                f"{int(counts_by_date.get(accounting.signal_date, {}).get('kept', 0))} "
                f"row(s) of closed accounting. Erasing that part would destroy closed "
                f"evidence and keeping it would leave panel.parquet carrying rows this "
                f"build can no longer account for. Panels are versioned, never "
                f"rewritten: build a new panel version instead."
            )
        counts_by_date[accounting.signal_date] = asdict(accounting)
    # counts.json FIRST, and atomically. The parts and the accounting are one
    # artifact; an interrupt must leave counts ahead of parts (the next run's
    # pending filter keys off part existence, so it re-prices and heals) rather
    # than parts ahead of counts (that date's accounting is then never
    # regenerated, and _aggregate's .get defaults read the hole as zero
    # unresolved drops — the attestation-friendly direction). Stock-Vault's own
    # writer has used an atomic write for exactly this reason.
    _atomic_write_text(counts_path, json.dumps(counts_by_date, indent=2, sort_keys=True) + "\n")
    for signal_date, part in sorted(new_parts.items()):
        part.to_parquet(out_dir / f"{signal_date.isoformat()}.parquet", index=False)
        result.parts_written += 1
        result.rows_written += len(part)

    _aggregate(counts_by_date, result)
    if result.cost_inconsistent_dates:
        raise SignalPanelError(
            f"{signal} mixes cost models or position sizes across signal dates: "
            f"{', '.join(result.cost_inconsistent_dates)} disagree with the rest of "
            f"the panel (model {result.cost_model_id!r}, notional "
            f"{result.cost_position_notional_usd!r}). round_trip_cost_bps is a "
            "function of size, so a panel priced at two sizes carries two different "
            "measurements under one column name and its mean net spread describes "
            "neither. Parts are immutable: build a new panel version at one size "
            "instead of extending this one at another."
        )

    parts = sorted(
        path
        for path in out_dir.glob("*.parquet")
        if path.name != "panel.parquet" and _is_iso_stem(path.stem)
    )
    # The rollup and the accounting must be ONE source of truth. Every whole-panel
    # number and all three attestations come from counts.json; every row in
    # panel.parquet comes from a filesystem glob. Nothing used to reconcile them,
    # so a part on disk with no counts entry — or a counts entry whose kept total
    # no longer matches the part's rows — silently flipped attestations True over
    # rows the ledger had stopped accounting for.
    on_disk = {path.stem for path in parts}
    orphan_parts = sorted(on_disk - set(counts_by_date))
    if orphan_parts:
        raise SignalPanelError(
            f"{signal} has part file(s) with no entry in counts.json: "
            f"{', '.join(orphan_parts)}. Every whole-panel number and all three "
            f"attestations are computed from counts.json, so rows nothing accounts "
            f"for would be attested by numbers that never saw them. Re-run with "
            f"--rebuild to regenerate the missing accounting."
        )
    panel_path = out_dir / "panel.parquet"
    if parts:
        rollup = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        rollup = rollup.sort_values(["signal_date", "ticker"]).reset_index(drop=True)
        rows_by_date = rollup.groupby("signal_date").size().to_dict()
        divergent = sorted(
            f"{date}: panel {int(rows)} row(s) vs counts kept "
            f"{int(counts_by_date.get(str(date), {}).get('kept', 0))}"
            for date, rows in rows_by_date.items()
            if int(rows) != int(counts_by_date.get(str(date), {}).get("kept", 0))
        )
        if divergent:
            raise SignalPanelError(
                f"{signal} rollup and counts.json disagree on {len(divergent)} signal "
                f"date(s): {'; '.join(divergent)}. The attestations stamped on every "
                f"row of this rollup are computed from counts.json alone; refusing to "
                f"write a panel whose rows and whose accounting are different row sets."
            )
        for name, value in result.attestations.items():
            rollup[name] = value
        rollup.to_parquet(panel_path, index=False)
    elif panel_path.is_file():
        panel_path.unlink()

    # Composed once and reused for the catalog below, so the sidecar and the
    # manifest cannot drift into two different verdicts over one panel. Built
    # AFTER _aggregate: the band's evaluable-period count is a whole-panel
    # number read back from counts.json like every other one here.
    payload = build_payload(result)
    band = payload.get("adv_band")
    (out_dir / "build.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = vault.signal_panel_manifest(signal, version)
    write_vault_manifest(
        out_dir,
        license_note=(
            # Parenthesized deliberately: `+` binds tighter than `or`, so the
            # unbracketed form read as `license_note or (manifest_note +
            # clause)` and a caller-supplied note discarded the entire returns
            # clause — the Massive EOD, the per-ex-date dividend archive, the
            # stockanalysis.com delisted histories and "do not redistribute
            # rows". That clause is a fact about what these bytes CONTAIN, not
            # a restatement of the upstream manifest, so no caller may
            # supersede it; the sibling source_urls below unions for the same
            # reason.
            (license_note or str(manifest.get("license_note", "")))
            + " Forward returns joined from Massive (ex-Polygon) free-tier EOD closes, the "
            "vault's per-ex-date dividend archive and stockanalysis.com delisted histories; "
            "private archive, do not redistribute rows."
        ),
        source_urls=sorted(
            set(source_urls or [])
            | set(manifest.get("source_urls", []))
            | {"https://massive.dev/", "https://stockanalysis.com/"}
        ),
        extra={
            "signal": signal,
            "panel_version": version,
            "artifact": "evaluable_panel",
            "built_by": "stock_grader.signal_panel",
            "panel_schema_version": SIGNAL_PANEL_SCHEMA_VERSION,
            "observations_dataset": (
                f"data/signal_panels/{signal}/v{version}/observations (Stock-Vault)"
            ),
            "spec": result.spec,
            "periods": len(counts_by_date),
            "observations": result.observations,
            "kept_rows": result.kept_rows,
            "unresolved_rows": result.unresolved_rows,
            "unresolved_fraction": round(result.unresolved_fraction, 6),
            "survival_rate": round(result.survival_rate, 6),
            "periods_in_panel": result.periods_in_panel,
            "dividend_coverage": round(result.dividend_coverage, 6),
            "dividend_archive_months": result.dividend_archive_months,
            "pit_membership_coverage": round(result.pit_membership_coverage, 6),
            "cost_model_id": result.cost_model_id,
            "cost_position_notional_usd": result.cost_position_notional_usd,
            "cost_priced_rows": result.cost_priced_rows,
            "no_cost_estimate_rows": result.no_cost_estimate_rows,
            "cost_coverage": round(result.cost_coverage, 6),
            "capacity_truncated_rows": result.capacity_truncated_rows,
            "capacity_truncated_notional_fraction": round(
                result.capacity_truncated_notional_fraction, 6
            ),
            # Same block as build.json, and present only when the observation
            # dataset was banded. The evaluator opens whichever of the two it
            # can verify, so the §1.3 verdict must reach both or a reader of
            # the catalog alone would see an unqualified panel.
            **({} if band is None else {"adv_band": band}),
            "attestations": result.attestations,
        },
    )
    return panel_path


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write via tmp + ``os.replace``: a reader never sees a half-file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _is_iso_stem(stem: str) -> bool:
    try:
        dt.date.fromisoformat(stem)
    except ValueError:
        return False
    return True


def build_payload(result: SignalPanelResult) -> dict:
    """Flat sidecar JSON — additive keys only, never an envelope."""
    from .research_manifest import package_commit

    band = band_report(result)
    return {
        # Present ONLY on a banded panel. An unbanded panel's build.json is
        # byte-for-byte the shape it always was, so nothing downstream can read
        # an absent key as a passing verdict.
        **({} if band is None else {"adv_band": band}),
        "schema_version": SIGNAL_PANEL_SCHEMA_VERSION,
        "signal": result.signal,
        "panel_version": result.panel_version,
        "built_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # The GRADER's revision — this job runs with the vault as CWD, and the
        # field names the owner of forward-return semantics. See package_commit.
        "builder_commit": package_commit(),
        "return_semantics": {
            "owner": "stock_grader.signal_panel (single implementation)",
            "formula": "(P_end * split_factor + in-window cash) / P_entry - 1",
            "dividend_window": "(entry, exit] — entry-exclusive, exit-inclusive",
            "exit_chain": "market_eod -> delisted_archive -> last_listed_close",
            "total_return_coverage_bar": TOTAL_RETURN_COVERAGE_BAR,
        },
        "spec": result.spec,
        "observation_periods": result.observation_periods,
        "observations": result.observations,
        "kept_rows": result.kept_rows,
        "unresolved_rows": result.unresolved_rows,
        "unresolved_fraction": result.unresolved_fraction,
        "unresolved_tickers": result.unresolved_tickers,
        "unresolved_tickers_incomplete_dates": result.unresolved_tickers_incomplete_dates,
        "panel_observations": result.panel_observations,
        "no_start_price_rows": result.no_start_price_rows,
        "survival_rate": result.survival_rate,
        "periods_accounted": result.periods_accounted,
        "periods_in_panel": result.periods_in_panel,
        "dividend_coverage": result.dividend_coverage,
        "dividend_archive_months": result.dividend_archive_months,
        "pit_membership_coverage": result.pit_membership_coverage,
        # What priced this panel's trading costs, and at what size. A net
        # number whose cost model and position size are not on the artifact is
        # not reproducible: the same panel priced at $100k and at $10M are
        # different measurements with the same column name.
        "cost_model": {
            "cost_model_id": result.cost_model_id,
            "position_notional_usd": result.cost_position_notional_usd,
            "cost_priced_rows": result.cost_priced_rows,
            "no_cost_estimate_rows": result.no_cost_estimate_rows,
            "cost_coverage": result.cost_coverage,
            "capacity_truncated_rows": result.capacity_truncated_rows,
            "capacity_truncated_notional_fraction": (result.capacity_truncated_notional_fraction),
            "golden_vector_sha256": golden_vector_sha256(),
        },
        "attestations": result.attestations,
        # Two provenances, never mixed in one namespace: everything above is
        # whole-panel (aggregated from the persisted counts.json), everything
        # under last_run covers only what THIS run priced. counts.json holds
        # the per-signal-date accounting for every closed part.
        "whole_panel_accounting": "counts.json",
        "last_run": {
            "parts_written": result.parts_written,
            "rows_written": result.rows_written,
            "periods_priced": [asdict(p) for p in result.periods],
        },
        "refusal": result.refusal,
    }
