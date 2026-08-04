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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

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
    "SIGNAL_PANEL_SCHEMA_VERSION",
    "SIGNAL_PANEL_VERSION",
    "SignalPanelConfig",
    "SignalPanelError",
    "SignalPanelResult",
    "SignalPeriodAccounting",
    "build_signal_panel",
    "write_signal_panel",
]

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


class SignalPanelError(RuntimeError):
    """The join cannot proceed honestly (bad artifact, escaped path)."""


@dataclass(frozen=True, slots=True)
class SignalPanelConfig:
    split_tolerance: float = 0.01
    rebuild: bool = False


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
    kept: int = 0


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
    dividend_coverage: float = 0.0
    dividend_archive_months: int = 0
    pit_membership_coverage: float = 0.0
    attestations: dict[str, bool] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
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
    needed_days: set[dt.date] = set()
    for entry, exit_ in windows.values():
        needed_days.update(d for d in days if entry <= d <= exit_)
    if not needed_days:
        result.refusal = (
            "the vault EOD archive covers none of the pending return windows"
        )
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

    from .research_manifest import current_commit

    builder_commit = current_commit()
    new_parts: dict[dt.date, pd.DataFrame] = {}

    for signal_date in pending:
        entry, exit_ = windows[signal_date]
        frame = observations[signal_date]
        accounting = SignalPeriodAccounting(
            signal_date=signal_date.isoformat(),
            return_start=entry.isoformat(),
            return_end=exit_.isoformat(),
            observations=len(frame),
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
                result.unresolved_tickers.append(ticker)
                continue

            resolution = resolve_exit_price(ticker_bars, vault, ticker, entry, exit_)
            if resolution is None:
                accounting.unresolved_dropped += 1
                result.unresolved_tickers.append(ticker)
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
                elif in_window and factor != 1.0:
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
                    "overlapping_windows": bool(
                        getattr(observation, "overlapping_windows", False)
                    ),
                    "horizon_trading_days": int(
                        getattr(observation, "horizon_trading_days", 0)
                    ),
                    "vault_commit": str(getattr(observation, "vault_commit", "")),
                    "observation_schema_version": str(
                        getattr(observation, "schema_version", "")
                    ),
                    "panel_version": version,
                    "panel_schema_version": SIGNAL_PANEL_SCHEMA_VERSION,
                    "builder_commit": builder_commit,
                }
            )
            accounting.kept += 1

        result.periods.append(accounting)
        if rows:
            part = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
            new_parts[signal_date] = part
        log(
            f"{signal} {signal_date}: kept={accounting.kept} "
            f"no_entry={accounting.no_start_price_dropped} "
            f"unresolved={accounting.unresolved_dropped}"
        )

    result.unresolved_tickers = sorted(set(result.unresolved_tickers))
    return result, new_parts


# -- persistence ---------------------------------------------------------------


def _aggregate(counts_by_date: dict[str, dict], result: SignalPanelResult) -> None:
    """Whole-panel numbers from the PERSISTED per-date accounting.

    Closed parts are immutable, so their accounting can never be recomputed —
    the same discipline the vault applies to its own counts.json. Reading the
    file back means an incremental run and a full rebuild report identical
    whole-panel attestations.
    """
    kept = sum(int(entry.get("kept", 0)) for entry in counts_by_date.values())
    unresolved = sum(int(entry.get("unresolved_dropped", 0)) for entry in counts_by_date.values())
    covered = sum(int(entry.get("dividend_covered", 0)) for entry in counts_by_date.values())
    pit_rows = sum(
        int(entry.get("pit_membership_rows", 0)) for entry in counts_by_date.values()
    )
    considered = kept + unresolved
    result.kept_rows = kept
    result.unresolved_rows = unresolved
    result.unresolved_fraction = unresolved / considered if considered else 0.0
    result.dividend_coverage = covered / kept if kept else 0.0
    result.pit_membership_coverage = pit_rows / kept if kept else 0.0
    result.attestations = {
        # Zero outcome-dependent drops AND full point-in-time membership. Both
        # legs, or the panel says False and reports its coverage.
        "universe_is_pit": bool(
            kept > 0 and unresolved == 0 and pit_rows == kept
        ),
        "return_is_total": bool(
            result.dividend_archive_months
            and kept > 0
            and result.dividend_coverage >= TOTAL_RETURN_COVERAGE_BAR
        ),
        "delisting_return_included": bool(kept > 0 and unresolved == 0),
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
        counts_by_date[accounting.signal_date] = asdict(accounting)
    for signal_date, part in sorted(new_parts.items()):
        part.to_parquet(out_dir / f"{signal_date.isoformat()}.parquet", index=False)
        result.parts_written += 1
        result.rows_written += len(part)
    counts_path.write_text(
        json.dumps(counts_by_date, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    _aggregate(counts_by_date, result)

    parts = sorted(
        path
        for path in out_dir.glob("*.parquet")
        if path.name != "panel.parquet" and _is_iso_stem(path.stem)
    )
    panel_path = out_dir / "panel.parquet"
    if parts:
        rollup = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        rollup = rollup.sort_values(["signal_date", "ticker"]).reset_index(drop=True)
        for name, value in result.attestations.items():
            rollup[name] = value
        rollup.to_parquet(panel_path, index=False)
    elif panel_path.is_file():
        panel_path.unlink()

    (out_dir / "build.json").write_text(
        json.dumps(build_payload(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = vault.signal_panel_manifest(signal, version)
    write_vault_manifest(
        out_dir,
        license_note=(
            license_note
            or str(manifest.get("license_note", ""))
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
            "dividend_coverage": round(result.dividend_coverage, 6),
            "dividend_archive_months": result.dividend_archive_months,
            "pit_membership_coverage": round(result.pit_membership_coverage, 6),
            "attestations": result.attestations,
        },
    )
    return panel_path


def _is_iso_stem(stem: str) -> bool:
    try:
        dt.date.fromisoformat(stem)
    except ValueError:
        return False
    return True


def build_payload(result: SignalPanelResult) -> dict:
    """Flat sidecar JSON — additive keys only, never an envelope."""
    from .research_manifest import current_commit

    return {
        "schema_version": SIGNAL_PANEL_SCHEMA_VERSION,
        "signal": result.signal,
        "panel_version": result.panel_version,
        "built_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "builder_commit": current_commit(),
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
        "dividend_coverage": result.dividend_coverage,
        "dividend_archive_months": result.dividend_archive_months,
        "pit_membership_coverage": result.pit_membership_coverage,
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
