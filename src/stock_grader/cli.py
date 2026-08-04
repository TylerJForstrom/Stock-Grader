"""Command-line interface.

    stock-grader grade AAPL --profile value --weighting entropy --explain
    stock-grader rank --universe tickers.txt --profile quality --top 20
    stock-grader consensus AAPL MSFT JNJ
    stock-grader methods | metrics | profiles

Data comes from SEC EDGAR (free, keyless, verified). Prices are optional: pass ``--price`` or
``--price-dir`` to enable the valuation and risk pillars, or omit them and the grade is built from
the fundamentals that could be computed, with the shortfall reported rather than hidden.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.markdown import Markdown

# Importing these modules is what populates the registries.
from . import __version__, aggregate, normalize, weighting  # noqa: F401
from .backtest import BacktestConfig, backtest_to_markdown, evaluate_walk_forward
from .data.prices import (
    BenchmarkProvider,
    ChainedPriceProvider,
    CSVPriceProvider,
    PriceProvider,
    RiskFreeProvider,
    TiingoPriceProvider,
    YahooPriceProvider,
)
from .data.sec import SECClient, SECProvider
from .data.sec_bulk import SECBulkFacts
from .data.sec_prices import SECInsiderPriceProvider, check_price_share_basis, resolve_price
from .data.stockanalysis import StockAnalysisPriceProvider
from .data.synthetic import generate_prices
from .data.vault import VaultDataSource, VaultPriceProvider
from .journal import DEFAULT_JOURNAL_DIR as _DEFAULT_JOURNAL_DIR
from .journal import (
    JournalError,
    append_run,
    comparability_mismatches,
    diff_reports,
    membership_fingerprint,
    previous_letters,
    resolve_since_last,
    snapshot_members,
)
from .metrics import fundamental, models, sector_specific, statistical  # noqa: F401
from .peers import explicit_peers, select_peers
from .pipeline import GradeConfig, config_fingerprint, grade_universe
from .profiles import consensus_grade, get_profile, profile_names
from .registry import AGGREGATORS, METRICS, NORMALIZERS, WEIGHTINGS
from .report import (
    DISCLAIMER,
    rank_reports,
    render_consensus,
    render_ranking,
    render_report,
    to_consensus_markdown,
    to_json,
    to_markdown,
    to_ranking_markdown,
)
from .research import build_research_report, research_to_json, research_to_markdown
from .types import PitMode, SecuritySnapshot
from .weighting import WEIGHT_METHOD_INFO

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_UNIVERSE_HEADER_KEYS = frozenset(
    {"universe_id", "asof", "spec_sha256", "source_sha256", "row_count"}
)


@dataclass(frozen=True, slots=True)
class UniverseSelection:
    """Tickers plus immutable provenance parsed from a committed universe artifact."""

    tickers: list[str]
    universe_id: str | None
    asof: date | None
    spec_sha256: str | None
    source_sha256: str | None
    path: str


def _empty_selection(path: str, tickers: list[str]) -> UniverseSelection:
    return UniverseSelection(tickers, None, None, None, None, path)


console = Console()
# Progress, warnings and errors go to stderr so `--format json` yields a parseable document on
# stdout. A JSON mode that emits a banner first is not a JSON mode.
status_console = Console(stderr=True)

CLI_NORMALIZERS = [
    name for name in NORMALIZERS.names() if name not in {"piecewise", "double_sigmoid"}
]
CLI_WEIGHTINGS = [
    name
    for name in WEIGHTINGS.names()
    if not WEIGHT_METHOD_INFO.get(name, {}).get("needs_returns", False)
    and name not in {"fixed", "ahp", "rank_order_centroid"}
]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _default_universe_path() -> Path | None:
    """Locate the bundled default peer list, whether running from a checkout or an install."""
    # The packaged copy first: it is the only one that exists after a plain `pip install`, and
    # without it every install outside a source checkout graded against a universe of one, which
    # makes every cross-sectional score a flat 50 and every grade N/A.
    candidates = [
        Path(__file__).resolve().parent / "config" / "universe_default.txt",
        Path(__file__).resolve().parent.parent.parent / "config" / "universe_default.txt",
        Path.cwd() / "config" / "universe_default.txt",
    ]
    return next((p for p in candidates if p.exists()), None)


_FOUNDRY_RAW_URL = "https://raw.githubusercontent.com/TylerJForstrom/Stock-Data/main"


def _load_universe_selection(path: str) -> UniverseSelection:
    """Load a ticker file and any immutable provenance carried in comment headers."""
    # "foundry:" pulls the listed-exchange universe from the Stock-Data
    # foundry's daily snapshot (manifest-verified). Forms:
    #   foundry:                      -> the public repo via raw URL
    #   foundry:C:/path/to/Stock-Data -> a local clone
    #   foundry:https://...           -> an explicit raw base URL
    if path.startswith("foundry:"):
        from .data.foundry import FoundryDataSource

        target = path[len("foundry:") :].strip()
        if not target:
            source = FoundryDataSource(url_base=_FOUNDRY_RAW_URL)
        elif target.lower().startswith(("http://", "https://")):
            source = FoundryDataSource(url_base=target)
        else:
            source = FoundryDataSource(root=target)
        return _empty_selection(path, source.universe_tickers())

    text = Path(path).read_text(encoding="utf-8")
    tickers: list[str] = []
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            comment = stripped.removeprefix("#").strip()
            parsed: tuple[str, str] | None = None
            for separator in (":", "="):
                if separator in comment:
                    key, value = comment.split(separator, 1)
                    parsed = (key.strip().lower(), value.strip())
                    break
            if parsed is not None and parsed[0] in _UNIVERSE_HEADER_KEYS:
                key, value = parsed
                if key in metadata and metadata[key] != value:
                    raise ValueError(f"universe header repeats {key} with a different value")
                metadata[key] = value
            continue
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        tickers.extend(
            part.strip().upper() for part in content.replace(",", " ").split() if part.strip()
        )

    if not metadata:
        return _empty_selection(path, tickers)
    missing = sorted(_UNIVERSE_HEADER_KEYS - set(metadata))
    if missing:
        raise ValueError("universe provenance header is missing: " + ", ".join(missing))
    if not metadata["universe_id"]:
        raise ValueError("universe_id cannot be empty")
    try:
        selection_asof = date.fromisoformat(metadata["asof"])
    except ValueError as exc:
        raise ValueError("universe header asof must be YYYY-MM-DD") from exc
    for key in ("spec_sha256", "source_sha256"):
        if not _SHA256.fullmatch(metadata[key]):
            raise ValueError(f"universe header {key} must be 64 hexadecimal characters")
    try:
        row_count = int(metadata["row_count"])
    except ValueError as exc:
        raise ValueError("universe header row_count must be an integer") from exc
    if row_count < 0 or row_count != len(tickers):
        raise ValueError(
            f"universe header row_count={row_count} does not match {len(tickers)} tickers"
        )
    if len(set(tickers)) != len(tickers):
        raise ValueError("provenance-carrying universe contains duplicate tickers")
    return UniverseSelection(
        tickers=tickers,
        universe_id=metadata["universe_id"],
        asof=selection_asof,
        spec_sha256=metadata["spec_sha256"],
        source_sha256=metadata["source_sha256"],
        path=path,
    )


def _load_universe(path: str) -> list[str]:
    """Backward-compatible ticker-only universe loader."""
    return _load_universe_selection(path).tickers


def _validate_universe_selection_asof(selection: UniverseSelection, requested_asof: date) -> None:
    """Refuse future-selected membership and universe artifacts older than one year."""
    if selection.asof is None:
        return
    if requested_asof < selection.asof:
        raise SystemExit(
            f"universe {selection.path} was selected as of {selection.asof} and cannot be used "
            f"for earlier signal date {requested_asof}"
        )
    age_days = (requested_asof - selection.asof).days
    if age_days > 365:
        raise SystemExit(
            f"universe {selection.path} is {age_days} days old for signal date {requested_asof}; "
            "rebuild the quarterly universe artifact"
        )


def _resolve_peers(args: argparse.Namespace, tickers: list[str]) -> list[str]:
    """Decide what to compare against.

    A cross-sectional grade is meaningless without a peer group, so an explicit ``--universe`` wins,
    the bundled default list is the fallback, and ``--no-peers`` opts out with the consequence
    stated plainly in the report rather than silently producing a flat-50 grade.
    """
    if args.universe:
        selection = _load_universe_selection(args.universe)
        requested_asof = date.fromisoformat(args.asof) if args.asof else date.today()
        _validate_universe_selection_asof(selection, requested_asof)
        return selection.tickers
    if args.no_peers:
        return []
    if args.asof:
        asof = date.fromisoformat(args.asof)
        if (date.today() - asof).days > 365:
            # The bundled list is 82 companies that are large TODAY, so using it to grade a past
            # date compares that date's company against a peer group selected for having survived
            # to now. Of 7,018 filers in late 2015, 45.5% still filed a decade later, and the
            # survivors had median ROA of +1.01% against -6.15% for the rest — the grader then
            # reports where a company sits on exactly profitability and health, with the left tail
            # deleted.
            raise SystemExit(
                f"config/universe_default.txt is a survivor list built from today's listings, so "
                f"it cannot be a peer group for --asof {asof}. Pass --universe with a list "
                f"constructed as of that date (CIKs preferred — tickers get reused)."
            )
    path = _default_universe_path()
    if path is None:
        status_console.print("[yellow]no default universe found; grading without peers[/yellow]")
        return []
    peers = _load_universe(str(path))
    status_console.print(
        f"[dim]comparing against {len(peers)} default peers "
        f"(--universe FILE to choose your own, --no-peers to skip)[/dim]"
    )
    return peers


def _sec_provider_from_args(args: argparse.Namespace, ticker_count: int) -> SECProvider:
    """Build one SEC client and optionally share it with the bulk Companyfacts reader."""
    client = SECClient(
        cache_dir=args.cache_dir,
        contact=args.contact,
        offline=args.no_network,
    )
    mode = getattr(args, "bulk_facts", "auto")
    if mode not in {"auto", "always", "never"}:
        raise ValueError(f"unknown --bulk-facts mode: {mode}")
    use_bulk = mode == "always" or (mode == "auto" and ticker_count >= 200 and not args.no_network)
    if not use_bulk:
        return SECProvider(client)
    bulk_cache = Path(args.cache_dir).resolve() / "bulk" if args.cache_dir else None
    bulk = SECBulkFacts(client, cache_dir=bulk_cache)
    # Validate/download once per command, never inside the per-ticker exception boundary.
    bulk.ensure(refresh=bool(args.refresh))
    return SECProvider(client, bulk=bulk)


def _price_providers_from_args(args: argparse.Namespace) -> list[PriceProvider]:
    """Build the dense-price chain without hiding which source the user selected.

    ``auto`` preserves the historical CSV/opt-in StockAnalysis/Yahoo behavior and now makes the
    already-shipped Tiingo provider reachable when ``TIINGO_API_KEY`` is configured.
    """
    mode = getattr(args, "price_provider", "auto")
    price_dir = getattr(args, "price_dir", None)
    stockanalysis = bool(getattr(args, "stockanalysis", False))
    vault_root = getattr(args, "vault", None)
    no_network = bool(getattr(args, "no_network", False))

    if price_dir and mode not in ("auto", "csv"):
        raise ValueError("--price-dir requires --price-provider auto or csv")
    if stockanalysis and mode not in ("auto", "stockanalysis"):
        raise ValueError("--stockanalysis cannot be combined with another explicit price provider")
    if mode == "csv" and not price_dir:
        raise ValueError("--price-provider csv requires --price-dir")
    if mode == "sec" and not bool(getattr(args, "sec_prices", True)):
        raise ValueError("--price-provider sec conflicts with --no-sec-prices")
    if no_network and mode in ("tiingo", "stockanalysis", "yahoo"):
        raise ValueError(f"--price-provider {mode} conflicts with --no-network")

    providers: list[PriceProvider] = []
    if vault_root:
        vault_cache = (
            Path(args.cache_dir).resolve() / "vault" if args.cache_dir else None
        )
        providers.append(VaultPriceProvider(VaultDataSource(vault_root), cache_dir=vault_cache))
    if mode == "auto":
        if price_dir:
            providers.append(CSVPriceProvider(price_dir))
        if stockanalysis and not no_network:
            # Opt-in: an undocumented endpoint of a commercial site. See that module's docstring.
            providers.append(
                StockAnalysisPriceProvider(cache_dir=args.cache_dir, contact=args.contact)
            )
        if not no_network:
            providers.extend((TiingoPriceProvider(), YahooPriceProvider()))
    elif mode == "csv":
        providers.append(CSVPriceProvider(price_dir))
    elif mode == "tiingo":
        providers.append(TiingoPriceProvider())
    elif mode == "stockanalysis":
        providers.append(StockAnalysisPriceProvider(cache_dir=args.cache_dir, contact=args.contact))
    elif mode == "yahoo":
        providers.append(YahooPriceProvider())
    # ``sec`` supplies only a sparse scalar below; ``none`` disables the dense price chain.
    return providers


def _apply_resolved_price(snapshot: SecuritySnapshot, found: dict) -> None:
    """Attach a sparse-price result without promoting a lower bound to an exact price."""
    snapshot.meta["price_source"] = found["source"]
    snapshot.meta["price_date"] = found["date"].isoformat()
    snapshot.meta["price_age_days"] = found["age_days"]
    if found.get("valuation_eligible", True):
        snapshot.price = found["price"]
        snapshot.meta.pop("valuation_price_rejected", None)
    else:
        snapshot.price = None
        snapshot.meta["price_lower_bound"] = found["price"]
        snapshot.meta["valuation_price_rejected"] = "public_float_lower_bound"
    fraction = found.get("non_affiliate_fraction")
    if fraction is not None:
        snapshot.meta["non_affiliate_fraction"] = round(fraction, 4)
    if found["age_days"] > 60:
        snapshot.warnings.append(
            f"price is {found['age_days']} days old ({found['source']}, "
            f"{found['date']}); valuation metrics are stale by that much"
        )
    if found["source"] == "public_float_lower_bound":
        snapshot.warnings.append(
            "SEC public float implies only a LOWER-BOUND price because affiliate holdings are "
            "excluded; the bound is evidence only and all exact valuation metrics are N/A"
        )


def _apply_yahoo_basis_gate(
    snapshot: SecuritySnapshot,
    basis_check: dict | None,
    *,
    historical_asof: bool,
    basis_reconciled: bool,
) -> None:
    """Quarantine a Yahoo scalar unless its split basis is safe for the requested date.

    The public-float check can prove a contradiction but cannot prove compatibility because a
    large affiliate stake can mask a split factor.  Yahoo historical closes are on today's split
    basis, so a historical scalar remains unverified even when no contradiction is visible.
    """
    if basis_check is not None:
        snapshot.meta["price_share_basis_check"] = basis_check
    mismatch = basis_check is not None and basis_check["status"] == "mismatch"
    if not mismatch and not historical_asof and basis_reconciled:
        return
    rejected_price = snapshot.price
    snapshot.price = None
    reason = "split_basis_mismatch" if mismatch else "split_basis_unverified"
    snapshot.meta["valuation_price_rejected"] = reason
    snapshot.meta["rejected_dense_price"] = rejected_price
    if mismatch:
        snapshot.warnings.append(
            "Yahoo price and historical DEI shares are on incompatible split bases; daily "
            "history is retained for return metrics, but valuation metrics are unavailable"
        )
    elif historical_asof:
        snapshot.warnings.append(
            "Yahoo historical closes use today's split basis and the point-in-time DEI share "
            "basis could not be affirmatively reconciled; daily history is retained for return "
            "metrics, but exact valuation metrics are N/A"
        )
    else:
        snapshot.warnings.append(
            "Yahoo's current price could not be reconciled to the dated DEI share basis with "
            "explicit split events; daily history is retained for return metrics, but exact "
            "valuation metrics are N/A"
        )


def _reconcile_current_yahoo_share_basis(
    snapshot: SecuritySnapshot,
    frame: pd.DataFrame,
    *,
    historical_asof: bool,
) -> bool:
    """Rebase current-run DEI shares with Yahoo's explicit intervening split events."""
    if historical_asof:
        return False
    raw_events = frame.attrs.get("split_events")
    shares_date = snapshot.meta.get("shares_date")
    shares = snapshot.shares_outstanding
    if not isinstance(raw_events, list) or shares_date is None or shares is None or shares <= 0:
        return False
    try:
        observed = pd.Timestamp(shares_date).normalize()
        first_bar = pd.Timestamp(frame.index.min()).normalize()
    except (TypeError, ValueError):
        return False
    if first_bar > observed:
        return False

    events: list[tuple[pd.Timestamp, float]] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            return False
        try:
            event_date = pd.Timestamp(raw["date"]).normalize()
            factor = float(raw["factor"])
        except (KeyError, TypeError, ValueError):
            return False
        if not math.isfinite(factor) or factor <= 0:
            return False
        events.append((event_date, factor))

    factor_since_observation = math.prod(
        factor for event_date, factor in events if event_date > observed
    )
    if (
        not math.isfinite(factor_since_observation)
        or not 0.001 <= factor_since_observation <= 1_000
    ):
        return False
    if factor_since_observation != 1.0:
        snapshot.shares_outstanding = float(shares * factor_since_observation)
        snapshot.meta["shares_split_rebased_factor"] = factor_since_observation
        snapshot.warnings.append(
            "DEI shares were rebased onto Yahoo's current split basis using explicit split events"
        )

    history = snapshot.meta.get("shares_history")
    if isinstance(history, pd.Series) and not history.empty:
        adjusted = history.copy().astype("float64")
        for history_date in adjusted.index:
            timestamp = pd.Timestamp(history_date).normalize()
            factor = math.prod(
                event_factor for event_date, event_factor in events if event_date > timestamp
            )
            adjusted.loc[history_date] = float(adjusted.loc[history_date]) * factor
        snapshot.meta["shares_history_price_basis"] = adjusted

    snapshot.meta["yahoo_share_basis_reconciliation"] = {
        "status": "reconciled",
        "shares_observation_date": observed.date().isoformat(),
        "cumulative_split_factor": factor_since_observation,
        "split_events_considered": [
            {"date": event_date.date().isoformat(), "factor": factor}
            for event_date, factor in events
            if event_date > observed
        ],
    }
    return True


def _build_snapshots(
    tickers: list[str],
    args: argparse.Namespace,
    *,
    provider: SECProvider,
) -> list[SecuritySnapshot]:
    """Fetch fundamentals for each ticker and attach prices where available."""
    asof = date.fromisoformat(args.asof) if args.asof else date.today()
    pit_mode = PitMode.PIT if args.pit else PitMode.LATEST
    price_providers = _price_providers_from_args(args)
    prices = ChainedPriceProvider(price_providers) if price_providers else None

    risk_free = None
    benchmark = None
    if not args.no_network:
        risk_free = RiskFreeProvider(cache_dir=args.cache_dir).get("3m", refresh=args.refresh)
        # Without this, beta / capm_alpha / idiosyncratic_volatility can never fire at all.
        benchmark = BenchmarkProvider(cache_dir=args.cache_dir).get(
            args.benchmark, refresh=args.refresh
        )

    # SEC insider-transaction prices: the only price source reachable without an API key.
    # Sparse (a few dates per quarter), which is enough for valuation but not for the daily
    # statistics, so it sets `price` and deliberately leaves `prices` unset.
    insider = None
    if args.sec_prices and not args.no_network:
        insider = SECInsiderPriceProvider(cache_dir=args.cache_dir, contact=args.contact)
        with console.status("[dim]loading SEC insider-transaction prices…[/dim]"):
            insider.load(asof=asof)
        status_console.print(
            f"[dim]SEC insider prices: {insider.coverage(asof=asof)} tickers[/dim]"
        )

    manual_prices = {}
    for entry in args.price or []:
        if "=" in entry:
            ticker, value = entry.split("=", 1)
            manual_prices[ticker.strip().upper()] = float(value)
        elif len(tickers) == 1:
            manual_prices[tickers[0]] = float(entry)

    # Foundry corporate actions: reconstructed dividends-per-share used as a
    # fallback for dividend metrics when the XBRL cash-flow tag is absent.
    foundry = None
    if getattr(args, "foundry", None):
        from .data.foundry import FoundryDataSource, FoundryError

        target = str(args.foundry).strip()
        try:
            if target.lower().startswith(("http://", "https://")):
                foundry = FoundryDataSource(url_base=target)
            else:
                foundry = FoundryDataSource(root=target)
            foundry.dividends()  # fail fast on contract violations
        except FoundryError as exc:
            # FAIL CLOSED: --foundry was explicitly requested, so a broken or
            # tampered foundry must stop the run, not quietly grade without it.
            # The old console-line-and-continue path meant a hash mismatch — the
            # exact thing the manifest contract exists to catch — produced a
            # panel identical to one graded with no foundry at all, and nothing
            # recorded the difference.
            console.print(f"[red]foundry contract violation: {exc}[/red]")
            raise SystemExit(2) from exc

    snapshots: list[SecuritySnapshot] = []
    status = status_console.status("[dim]loading securities…[/dim]") if len(tickers) > 1 else None
    if status:
        status.start()
    for i, ticker in enumerate(tickers, 1):
        if status:
            status.update(f"[dim]loading {ticker} ({i}/{len(tickers)})…[/dim]")
        try:
            identifier = ticker.removeprefix("CIK:")
            if identifier.isdigit():
                snapshot = provider.fetch_by_cik(
                    identifier,
                    ticker=ticker,
                    asof=asof,
                    pit_mode=pit_mode,
                    refresh=args.refresh,
                )
            else:
                snapshot = provider.fetch(
                    ticker, asof=asof, pit_mode=pit_mode, refresh=args.refresh
                )
        except Exception as exc:
            status_console.print(
                f"[yellow]{ticker}: skipped ({type(exc).__name__}: {exc})[/yellow]"
            )
            continue
        if prices is not None:
            frame = prices.get(ticker, end=asof)
            if frame is not None:
                snapshot.prices = frame
                snapshot.meta["price_source"] = prices.last_source
                # Market cap wants the TRADED price, not the adjusted one. adj_close sits below
                # the raw close by the whole cumulative dividend adjustment, and that gap grows the
                # further back --asof reaches: AT&T's 2018-07-25 bar is close 30.25 against
                # adjusted 14.09, a 53% deflation. Using it would deflate every historical multiple
                # in near-exact proportion to dividend yield, so a valuation backtest would
                # "discover" that high-yield stocks are cheap. The statistical metrics keep
                # adj_close, where the dividend adjustment is exactly what you want.
                column = "close" if frame["close"].notna().any() else "adj_close"
                snapshot.price = float(frame[column].dropna().iloc[-1])
                if column == "adj_close":
                    snapshot.meta["price_is_adjusted"] = True
                    snapshot.warnings.append(
                        "no raw close available; market cap uses the adjusted close and is "
                        "understated by cumulative dividends"
                    )
                if prices.last_source == "yahoo":
                    historical_asof = asof != date.today()
                    basis_reconciled = _reconcile_current_yahoo_share_basis(
                        snapshot,
                        frame,
                        historical_asof=historical_asof,
                    )
                    basis_check = check_price_share_basis(
                        frame[column],
                        snapshot.meta.get("public_float_history"),
                        snapshot.meta.get(
                            "shares_history_price_basis",
                            snapshot.meta.get("shares_history"),
                        ),
                    )
                    _apply_yahoo_basis_gate(
                        snapshot,
                        basis_check,
                        historical_asof=historical_asof,
                        basis_reconciled=basis_reconciled,
                    )
            elif prices.last_rejections:
                snapshot.meta["price_rejections"] = prices.last_rejections
                stale_sources: list[str] = []
                for rejection in prices.last_rejections:
                    quality = rejection.get("price_quality")
                    if isinstance(quality, dict) and quality.get("stale"):
                        stale_sources.append(str(rejection.get("provider")))
                if stale_sources:
                    snapshot.warnings.append(
                        "stale dense price history refused from "
                        + ", ".join(stale_sources)
                        + "; no stale close was used as the current valuation price"
                    )
        if args.synthetic_prices and snapshot.prices is None:
            snapshot.prices = generate_prices(
                ticker, n_days=1300, end=pd.Timestamp(asof), synthetic=True
            )
            snapshot.meta["synthetic_prices"] = True
            if snapshot.price is None:
                snapshot.price = float(snapshot.prices["adj_close"].iloc[-1])
        if snapshot.price is None and insider is not None:
            found = resolve_price(
                ticker,
                asof=asof,
                insider=insider,
                public_float=snapshot.public_float,
                float_history=snapshot.meta.get("public_float_history"),
                shares_outstanding=snapshot.shares_outstanding,
                max_age_days=args.max_price_age,
            )
            if found is not None:
                _apply_resolved_price(snapshot, found)
        if ticker in manual_prices:
            snapshot.price = manual_prices[ticker]
            snapshot.meta["price_source"] = "manual"
            snapshot.meta.pop("valuation_price_rejected", None)
        snapshot.risk_free = risk_free
        if benchmark is not None:
            snapshot.benchmark = benchmark
            snapshot.meta["benchmark"] = args.benchmark
            # A price index excludes dividends, so alpha against it is overstated by roughly
            # beta x the index dividend yield.
            snapshot.meta["benchmark_is_price_only"] = True
        if foundry is not None:
            # trailing_dps returns None for a ticker the foundry has no usable
            # rows for — absence is unknown, never zero. A FoundryError here is
            # a contract violation mid-run and propagates; the old blanket
            # except swallowed hash mismatches into silent absence.
            dps = foundry.trailing_dps(ticker)
            snapshot.meta["foundry_status"] = "verified"
            if dps is not None:
                snapshot.meta["foundry_dps_ttm"] = dps
                snapshot.meta["foundry_dps_source"] = "stock-data corporate_actions"
        snapshots.append(snapshot)
    if status:
        status.stop()

    unresolved = [s.ticker for s in snapshots if s.cik is None]
    if unresolved:
        # One aggregate note rather than N scrolling warnings.
        status_console.print(
            f"[yellow]{len(unresolved)}/{len(snapshots)} tickers are absent from SEC's ticker "
            f"map ({', '.join(unresolved[:6])}{'…' if len(unresolved) > 6 else ''}). That map "
            f"lists only currently-listed issuers, so delisted companies are missing and reused "
            f"tickers resolve to the survivor — BBBY resolves to the entity that bought the brand, "
            f"not the retailer that failed.[/yellow]"
        )
    return snapshots


def _config_from_args(args: argparse.Namespace, *, profile: str | None = None) -> GradeConfig:
    overrides = {}
    if args.weighting:
        overrides["metric_weighting"] = args.weighting
        overrides["pillar_weighting"] = args.weighting
        overrides["pillar_weights"] = {}  # a chosen method must not be overridden by fixed weights
    if args.normalizer:
        overrides["normalizer"] = args.normalizer
    if args.aggregator:
        overrides["pillar_aggregator"] = args.aggregator
    if args.sector_neutral:
        overrides["sector_neutral"] = True
    if args.curve:
        overrides["curve"] = args.curve
    if args.rho is not None:
        overrides["aggregator_kwargs"] = {"rho": args.rho}
    return get_profile(profile or args.profile, **overrides)


def _journal_dir_from_args(args: argparse.Namespace) -> Path | None:
    """The run-journal directory, or None when journaling is off for this run."""
    if getattr(args, "no_journal", False):
        return None
    return Path(getattr(args, "journal_dir", None) or _DEFAULT_JOURNAL_DIR).expanduser()


def cmd_grade(args: argparse.Namespace) -> int:
    tickers = [t.upper() for t in args.tickers]
    peers = _resolve_peers(args, tickers)
    all_tickers = list(dict.fromkeys(tickers + peers))
    provider = _sec_provider_from_args(args, len(all_tickers))
    snapshots = _build_snapshots(all_tickers, args, provider=provider)
    if not snapshots:
        console.print("[red]no securities could be loaded[/red]")
        return 2

    config = _config_from_args(args)
    journal_dir = _journal_dir_from_args(args)
    previous: dict[str, str] = {}
    if journal_dir is not None and getattr(args, "hysteresis", False):
        # Letters from the newest journaled run in the same comparability
        # regime (config + peer-set membership) seed boundary hysteresis, so a
        # score drifting a fraction of a point does not flip the letter on
        # every refresh. Opt-in per SPEC design decision D9: prior state is an
        # input, so it must be asked for, never silently mixed in from local
        # state. No comparable run means hysteresis simply stays out.
        previous = previous_letters(
            journal_dir,
            config_fingerprint=config_fingerprint(config),
            membership_fingerprint=membership_fingerprint(snapshot_members(snapshots)),
        )
    reports = grade_universe(snapshots, config, previous_letters=previous or None)
    selected = {t: reports[t] for t in tickers if t in reports}
    if not selected:
        console.print("[red]nothing to grade[/red]")
        return 2

    if args.format == "json":
        print(to_json(selected if len(selected) > 1 else next(iter(selected.values()))))
    elif args.format == "md":
        print("\n\n---\n\n".join(to_markdown(r) for r in selected.values()))
    else:
        for report in selected.values():
            render_report(report, console, explain=args.explain)

    if journal_dir is not None:
        try:
            append_run(reports, journal_dir=journal_dir, command="grade")
        except (JournalError, OSError) as exc:
            # The grade itself is unaffected; say why the journal was not.
            status_console.print(f"[yellow]run not journaled: {exc}[/yellow]")
    return 0 if any(report.graded for report in selected.values()) else 3


def _fmt_delta_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}" if value else "0.00"


def _fmt_number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def cmd_diff(args: argparse.Namespace) -> int:
    """Compare a ticker's newest journaled grade against the run before it.

    Refuses a baseline from a different comparability regime (config or
    peer-set membership change): presenting cross-regime movement as a delta
    is exactly the misreading fingerprints exist to prevent. Data-vintage
    movement between comparable runs is what the diff reports.
    """
    journal_dir = Path(args.journal_dir or _DEFAULT_JOURNAL_DIR).expanduser()
    ticker = args.ticker.upper()
    try:
        (base_path, baseline), (cur_path, current) = resolve_since_last(journal_dir, ticker)
    except JournalError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    drift = comparability_mismatches(baseline, current)
    if drift and not args.allow_fingerprint_drift:
        for reason in drift:
            console.print(f"[red]{reason}[/red]")
        console.print(
            "[red]two scores are comparable only when fingerprints match; pass "
            "--allow-fingerprint-drift to accept a regime break[/red]"
        )
        return 2

    try:
        payload = diff_reports(baseline, current, ticker)
    except KeyError:
        console.print(f"[red]{ticker} missing from a resolved run — journal inconsistent[/red]")
        return 2
    payload["fingerprint_drift"] = drift
    payload["baseline"]["path"] = str(base_path)
    payload["current"]["path"] = str(cur_path)
    payload["disclaimer"] = DISCLAIMER

    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    from rich.box import SIMPLE
    from rich.table import Table
    from rich.text import Text

    for reason in drift:
        console.print(f"[yellow]fingerprint drift accepted: {reason}[/yellow]")
    console.print(
        f"[bold white]{ticker}[/bold white]  profile [cyan]{payload['profile']}[/cyan]  "
        f"{payload['baseline']['asof']} → {payload['current']['asof']}"
    )
    console.print(
        f"[dim]baseline {payload['baseline']['recorded_at_utc']}  →  "
        f"current {payload['current']['recorded_at_utc']}   "
        f"config {str(payload['current']['config_fingerprint'])[:12]}…[/dim]"
    )
    letter = payload["letter"]
    score = payload["score"]
    console.print(
        f"letter {letter['from']} → {letter['to']}"
        + ("" if letter["changed"] else "  (unchanged)")
        + f"   score {_fmt_number(score['from'])} → {_fmt_number(score['to'])} "
        + f"(Δ {_fmt_delta_number(score['delta'])})"
    )
    percentile = payload["percentile"]
    coverage = payload["coverage"]
    console.print(
        f"[dim]percentile {_fmt_number(percentile['from'])} → {_fmt_number(percentile['to'])} "
        f"(Δ {_fmt_delta_number(percentile['delta'])})   "
        f"coverage {_fmt_number(coverage['from'])} → {_fmt_number(coverage['to'])}[/dim]"
    )

    if payload["pillars"]:
        table = Table(box=SIMPLE, title="Pillar deltas", title_justify="left", header_style="bold")
        table.add_column("pillar", style="cyan")
        table.add_column("baseline", justify="right")
        table.add_column("current", justify="right")
        table.add_column("Δ", justify="right")
        for name, entry in payload["pillars"].items():
            table.add_row(
                name,
                _fmt_number(entry["from"]),
                _fmt_number(entry["to"]),
                _fmt_delta_number(entry["delta"]),
            )
        console.print(table)

    movers = [entry for entry in payload["metric_movers"] if entry["delta"]]
    if movers:
        shown = movers[:10]
        table = Table(
            box=SIMPLE,
            title="Metric contributions that moved the grade",
            title_justify="left",
            header_style="bold",
        )
        table.add_column("metric", style="cyan")
        table.add_column("baseline", justify="right")
        table.add_column("current", justify="right")
        table.add_column("Δ", justify="right")
        for entry in shown:
            table.add_row(
                entry["metric"],
                _fmt_number(entry["from"]),
                _fmt_number(entry["to"]),
                _fmt_delta_number(entry["delta"]),
            )
        console.print(table)
        if len(movers) > len(shown):
            console.print(f"[dim]{len(movers) - len(shown)} smaller mover(s) not shown[/dim]")
    else:
        console.print("[dim]no metric contribution moved between the two runs[/dim]")
    console.print(Text(DISCLAIMER, style="dim"))
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    selection = _load_universe_selection(args.universe)
    asof_arg = getattr(args, "asof", None)
    requested_asof = date.fromisoformat(asof_arg) if asof_arg else date.today()
    _validate_universe_selection_asof(selection, requested_asof)
    tickers = selection.tickers
    if not tickers:
        console.print("[red]universe file is empty[/red]")
        return 2
    provider = _sec_provider_from_args(args, len(tickers))
    snapshots = _build_snapshots(tickers, args, provider=provider)
    if not snapshots:
        console.print("[red]no securities could be loaded[/red]")
        return 2
    reports = grade_universe(snapshots, _config_from_args(args))
    if not reports:
        console.print("[red]grading produced no reports[/red]")
        return 2
    ordered = rank_reports(reports, top=args.top)
    limited = {report.ticker: report for report in ordered}
    if args.format == "json":
        print(to_json(limited))
    elif args.format == "md":
        print(to_ranking_markdown(reports, top=args.top))
    else:
        render_ranking(reports, console, top=args.top)
    return 0 if any(report.graded for report in reports.values()) else 3


def cmd_consensus(args: argparse.Namespace) -> int:
    tickers = [t.upper() for t in args.tickers]
    peers = _resolve_peers(args, tickers)
    identifiers = list(dict.fromkeys(tickers + peers))
    provider = _sec_provider_from_args(args, len(identifiers))
    snapshots = _build_snapshots(identifiers, args, provider=provider)
    # Pass the scoring flags through. Without this, --weighting/--normalizer/--rho/--curve and
    # --sector-neutral were accepted and silently discarded, so `consensus` answered a different
    # question than the one asked.
    overrides = {}
    if args.weighting:
        overrides["metric_weighting"] = args.weighting
        overrides["pillar_weighting"] = args.weighting
        overrides["pillar_weights"] = {}
    if args.normalizer:
        overrides["normalizer"] = args.normalizer
    if args.aggregator:
        overrides["pillar_aggregator"] = args.aggregator
    if args.sector_neutral:
        overrides["sector_neutral"] = True
    if args.curve:
        overrides["curve"] = args.curve
    if args.rho is not None:
        overrides["aggregator_kwargs"] = {"rho": args.rho}
    results = consensus_grade(snapshots, **overrides)
    selected = {t: results[t] for t in tickers if t in results}
    if not selected:
        console.print("[red]consensus produced no requested results[/red]")
        return 2
    if args.format == "json":
        payload = selected if len(selected) != 1 else next(iter(selected.values()))
        print(to_json(payload))
    elif args.format == "md":
        print(to_consensus_markdown(selected))
    else:
        render_consensus(selected, console)
        for result in selected.values():
            if not len(result.scores):
                report = next(iter(result.per_profile.values()), None)
                reason = (
                    next(iter(report.warnings), "no metrics could be computed") if report else ""
                )
                console.print(
                    f"\n[bold]{result.ticker}[/bold]: [yellow]not gradeable[/yellow] — {reason}"
                )
                continue
            console.print(f"\n[bold]{result.ticker}[/bold] by profile:")
            ordered_profiles = sorted(
                result.per_profile.items(),
                key=lambda item: (
                    not item[1].graded,
                    -item[1].score if math.isfinite(item[1].score) else 1e9,
                    item[0],
                ),
            )
            for name, report in ordered_profiles:
                score = f"{report.score:5.1f}" if math.isfinite(report.score) else "    —"
                excluded = "  [dim](excluded from consensus)[/dim]" if not report.graded else ""
                console.print(f"  {name:18} {report.letter:>3}  {score}{excluded}")
    return 0 if any(len(result.scores) for result in selected.values()) else 3


def cmd_research(args: argparse.Namespace) -> int:
    """Build one evidence-rich analyst dossier with an explicit peer manifest."""

    ticker = args.ticker.upper()
    peer_identifiers = _resolve_peers(args, [ticker])
    identifiers = list(dict.fromkeys([ticker, *peer_identifiers]))
    provider = _sec_provider_from_args(args, len(identifiers))
    snapshots = _build_snapshots(identifiers, args, provider=provider)
    by_ticker = {snapshot.ticker.upper(): snapshot for snapshot in snapshots}
    target = by_ticker.get(ticker)
    if target is None:
        console.print(f"[red]{ticker} could not be loaded[/red]")
        return 2

    candidates = [snapshot for snapshot in snapshots if snapshot is not target]
    universe_label = (
        f"explicit:{Path(args.universe).name}"
        if args.universe
        else "none"
        if args.no_peers
        else "bundled_current_survivor_universe"
    )
    if args.peer_mode == "explicit":
        peers, selection = explicit_peers(
            target,
            candidates,
            candidate_universe=universe_label,
        )
    else:
        peers, selection = select_peers(
            target,
            candidates,
            minimum=args.peer_min,
            maximum=args.peer_max,
            size_band_multiple=args.size_band,
            candidate_universe=universe_label,
        )

    dossier = build_research_report(
        target,
        peers,
        selection,
        _config_from_args(args),
        valuation_growth_rates=tuple(args.dcf_growth),
        valuation_discount_rate=args.discount_rate,
        valuation_terminal_growth=args.terminal_growth,
    )
    if args.format == "json":
        print(research_to_json(dossier))
    else:
        markdown = research_to_markdown(dossier)
        if args.format == "md":
            print(markdown)
        else:
            console.print(Markdown(markdown))
    return 0 if dossier.grade.graded else 3


def _refresh_freeze_manifests(
    out_dir: Path, profiles: list[str], frozen_names: dict[str, str]
) -> list[str]:
    """Catalog every requested profile directory that exists.

    ``frozen_names`` maps profile -> the part THIS run froze, so those entries
    carry ``hashed_at: "freeze"``; parts found already on disk are carried
    forward from the prior manifest or honestly marked ``backfill``. A part
    that cannot be cataloged (corrupt bytes) never stops the OTHER profiles'
    catalogs from being written: it is reported and returned so the caller can
    go red, and until it is investigated the stale-or-absent manifest makes
    every downstream consumer of that directory refuse.
    """
    from .frozen_manifest import refresh_frozen_manifest

    failures: list[str] = []
    for profile in profiles:
        directory = out_dir / profile
        if not directory.is_dir():
            continue
        written = frozen_names.get(profile)
        try:
            refresh_frozen_manifest(
                directory,
                frozen_now=frozenset({written}) if written else frozenset(),
            )
        except ValueError as exc:
            console.print(f"[red]{profile}: manifest not written: {exc}[/red]")
            failures.append(profile)
    return failures


def _load_panel_frame(path: Path) -> pd.DataFrame:
    """Read a score panel exactly as the backtest evaluator will see it.

    The read is manifest-verified: when a sibling ``manifest.json`` catalogs
    the file, its sha256 must match (mismatch refuses); when no manifest
    exists the panel loads with a warning — pre-convention directories are
    immutable, never rewritten, so they stay readable but unattested.
    """
    if not path.exists():
        raise ValueError(f"panel does not exist: {path}")
    from .frozen_manifest import verify_sibling_manifest

    verify_sibling_manifest(path)
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _backtest_spec(panel: pd.DataFrame, args: argparse.Namespace) -> dict:
    """The hypothesis identity a backtest run observes, for pre-registration.

    Covers what stays FIXED across scheduled re-evaluations of one declared
    hypothesis: the panel's identity columns (profile, scoring-config
    fingerprints, universe scope, horizon) and the evaluation parameters that
    change the estimator. Deliberately excluded: the data span (the accruing
    sample IS the point), per-freeze universe fingerprints (they hash each
    member's data vintage, so they drift monthly without the hypothesis
    changing), and bootstrap knobs (they shape the CI, not the trial). A
    scoring-config change DOES change the spec hash — a new configuration is
    honestly a new trial.
    """

    def column_values(name: str) -> list:
        if name not in panel:
            return []
        return sorted({str(v) for v in panel[name].dropna().unique()})

    return {
        "kind": "backtest_panel",
        "profiles": column_values("profile"),
        "config_fingerprints": column_values("config_fingerprint"),
        "universe_ids": column_values("universe_id"),
        "horizon_days": (
            sorted({int(v) for v in panel["horizon_days"].dropna().unique()})
            if "horizon_days" in panel
            else []
        ),
        "targets": ["forward_return"],
        "quantiles": int(args.quantiles),
        "min_cross_section": int(args.min_cross_section),
        "periods_per_year": int(args.periods_per_year),
        "transaction_cost_bps": float(args.transaction_cost_bps),
    }


def cmd_backtest(args: argparse.Namespace) -> int:
    """Evaluate a caller-supplied, frozen point-in-time score panel."""

    path = Path(args.panel)
    panel = _load_panel_frame(path)
    allow_mixed_universes = bool(getattr(args, "allow_mixed_universes", False))
    report = evaluate_walk_forward(
        panel,
        BacktestConfig(
            quantiles=args.quantiles,
            min_cross_section=args.min_cross_section,
            periods_per_year=args.periods_per_year,
            transaction_cost_bps=args.transaction_cost_bps,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_block_periods=args.bootstrap_block_periods,
            seed=args.seed,
        ),
        allow_mixed_universes=allow_mixed_universes,
    )
    failed_contract = [
        name
        for name, passed in report.input_contract.items()
        if not passed and not (allow_mixed_universes and name == "single_universe_id")
    ]
    if failed_contract and not args.allow_unverified_panel:
        raise ValueError(
            "panel fails the strict input contract ("
            + ", ".join(failed_contract)
            + "); add the documented attestation/identifier columns or pass "
            "--allow-unverified-panel for an explicitly caveated exploratory run"
        )
    # §6 significance wiring: every backtest run IS a trial. It is recorded in
    # the append-only ledger, and the edge verdict is deflated by every trial
    # the ledger has ever seen — the honest correction for "we kept the best
    # backtest". Deleting the ledger to reset the count is exactly the fraud
    # the SHA-256 chain in the manifest is designed to make visible.
    from .research_manifest import (
        ResearchRecord,
        append_record,
        current_commit,
        find_preregistration,
        load_manifest,
        preregistered_experiment,
        spec_sha256,
        trial_sharpes_by_experiment,
    )
    from .significance import assess_edge, per_period_sharpe

    net_spreads = [p.net_spread for p in report.periods]
    ledger_path = Path(getattr(args, "ledger", "research_ledger.jsonl"))
    prior = load_manifest(ledger_path) if ledger_path.exists() else []
    # Pre-registration lookup: when this run's observed spec was declared BEFORE
    # any evaluation (see `ledger-declare`), the run is a primary re-evaluation
    # of that ONE trial — recorded under the declaration's stable experiment
    # name so the collapse-to-latest denominator stays flat across scheduled
    # looks — never a new trial. No declaration (or a tampered one): behavior
    # is unchanged and the run is charged as a fresh trial.
    spec = _backtest_spec(panel, args)
    spec_hash = spec_sha256(spec)
    declaration = find_preregistration(prior, spec)
    experiment = (
        preregistered_experiment(spec) if declaration is not None else f"backtest:{path.name}"
    )
    # Only finite trial Sharpes deflate: a null/NaN sharpe (a panel too short to
    # compute one) is a trial with no usable statistic, and letting it through
    # makes stdev(trial_sharpes) NaN and therefore DSR=NaN on every future run.
    sharpe_by_experiment = trial_sharpes_by_experiment(prior)
    this_sharpe = per_period_sharpe(net_spreads) if len(net_spreads) >= 2 else float("nan")
    if declaration is not None:
        # REPLACE, never append: a re-measurement of the declared hypothesis
        # supersedes its previous look in the shared denominator.
        if math.isfinite(this_sharpe):
            sharpe_by_experiment[experiment] = this_sharpe
        trial_sharpes = list(sharpe_by_experiment.values())
    else:
        trial_sharpes = list(sharpe_by_experiment.values())
        if math.isfinite(this_sharpe):
            trial_sharpes.append(this_sharpe)
    significance = (
        assess_edge(
            net_spreads,
            trial_sharpes,
            periods_per_year=args.periods_per_year,
            bootstrap_seed=args.seed,
        )
        if len(net_spreads) >= 2
        else None
    )
    declaration_sha = (
        str(declaration.get("integrity_sha256", "")) if declaration is not None else None
    )
    append_record(
        ledger_path,
        ResearchRecord(
            experiment=experiment,
            market="us_equities",
            symbols=(
                [f"preregistration:{declaration_sha}"] if declaration_sha is not None else []
            ),
            targets=["forward_return"],
            horizons=[],
            trials=len(trial_sharpes),
            # Non-finite metrics are stored as None (JSON null), never NaN: a NaN
            # in the ledger is contagious across every later deflation.
            metrics={
                "per_period_sharpe": (this_sharpe if math.isfinite(this_sharpe) else None),
                "mean_net_spread": report.mean_net_spread,
                "mean_rank_ic": report.mean_rank_ic,
                "deflated_sharpe": (
                    significance.deflated_sharpe
                    if significance and math.isfinite(significance.deflated_sharpe)
                    else None
                ),
            },
            costs={"transaction_cost_bps": float(args.transaction_cost_bps)},
            benchmark="zero",
            leakage_controls=(
                "panel attestation contract: "
                + ("PASS" if not failed_contract else "FAILED " + ",".join(failed_contract))
                + (
                    f"; primary re-evaluation of pre-registered declaration "
                    f"{declaration_sha[:12]} (spec {spec_hash[:12]}); schedule-declared "
                    f"sequential look — disclosed peeking, not corrected"
                    if declaration_sha is not None
                    else ""
                )
            ),
            gate_passed=bool(significance and significance.significant),
            verdict=(
                ("PRIMARY (pre-registered) -- " if declaration_sha is not None else "")
                + (significance.verdict if significance else "insufficient periods")
            ),
            data_span=(
                f"{report.periods[0].signal_date}..{report.periods[-1].signal_date}"
                if report.periods
                else ""
            ),
            code_commit=current_commit(),
        ),
    )
    if declaration_sha is not None:
        status_console.print(
            f"[dim]primary re-evaluation of pre-registered spec {spec_hash[:12]} "
            f"recorded in {ledger_path} as {experiment} "
            f"(lifetime trials unchanged: {len(trial_sharpes)})[/dim]"
        )
    else:
        status_console.print(
            f"[dim]trial recorded in {ledger_path} (lifetime trials: {len(trial_sharpes)})[/dim]"
        )

    if args.format == "json":
        # Additive keys on the documented report schema - never an envelope.
        payload = json.loads(to_json(report))
        if significance is not None:
            payload["significance"] = json.loads(to_json(significance))
        payload["ledger"] = {"path": str(ledger_path), "lifetime_trials": len(trial_sharpes)}
        payload["ledger"]["preregistered"] = declaration_sha is not None
        payload["ledger"]["spec_sha256"] = spec_hash
        if declaration_sha is not None:
            payload["ledger"]["declaration_sha256"] = declaration_sha
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        markdown = backtest_to_markdown(report)
        if significance is not None:
            markdown += (
                "\n\n## Edge significance (deflated for every ledger trial)\n\n```\n"
                + significance.summary()
                + "\n```\n"
            )
        if args.format == "md":
            print(markdown)
        else:
            console.print(Markdown(markdown))
    return 0


# Stock-Vault's paper trader pins its pre-registered v1 rules to this profile,
# so its refusal breaks live trading while another profile's does not.
TRADED_PROFILE = "all_weather"


def _has_prior_panel(out_dir: Path, profile: str, signal_date: date) -> bool:
    """Has this profile ever frozen a panel on a date other than this one?"""
    directory = out_dir / profile
    if not directory.is_dir():
        return False
    return any(path.stem != signal_date.isoformat() for path in directory.glob("*.parquet"))


def cmd_freeze(args: argparse.Namespace) -> int:
    """Freeze today's scores into the append-only forward panel.

    Grades the universe and writes one immutable parquet per profile per signal
    date. Scores frozen BEFORE the future happens are the only backtest input
    that cannot possibly be overfit to it — every month of running this is a
    month of evidence money cannot buy later. Existing dates are never
    overwritten.

    ``--all-profiles`` grades the one set of snapshots under every registered
    profile. The SEC fetch and the snapshot build happen once either way, so
    the extra profiles cost no network calls at all, and in a few years the
    panels answer *which* style lens has an edge rather than merely whether one
    blend does. The deflated-Sharpe ledger already charges for the extra trials.
    """
    signal_date = date.fromisoformat(args.asof) if args.asof else date.today()
    if args.universe:
        selection = _load_universe_selection(args.universe)
        _validate_universe_selection_asof(selection, signal_date)
        tickers = selection.tickers
    else:
        tickers = _resolve_peers(args, [])
        selection = _empty_selection("bundled_current_survivor_universe", tickers)
    if not tickers:
        console.print("[red]no universe to freeze[/red]")
        return 2
    out_dir = Path(args.out)
    profiles = profile_names() if getattr(args, "all_profiles", False) else [args.profile]

    def panel_path(profile: str) -> Path:
        return out_dir / profile / f"{signal_date.isoformat()}.parquet"

    pending = []
    for profile in profiles:
        if panel_path(profile).exists():
            status_console.print(f"[dim]{panel_path(profile)} already frozen; nothing to do[/dim]")
        else:
            pending.append(profile)
    if not pending:
        # Even a freeze that froze nothing refreshes the catalogs: a crash
        # between a prior run's panel write and its manifest write, or a
        # directory that predates the manifest convention, heals here instead
        # of refusing downstream forever.
        return 2 if _refresh_freeze_manifests(out_dir, profiles, {}) else 0

    # Built once and graded under every pending profile: the whole point of the
    # multi-profile freeze is that the fetch is the expensive part.
    provider = _sec_provider_from_args(args, len(tickers))
    snapshots = _build_snapshots(tickers, args, provider=provider)
    if not snapshots:
        console.print("[red]no securities could be loaded[/red]")
        return 2

    from .research_manifest import current_commit

    commit = current_commit()
    refused: list[str] = []
    frozen_names: dict[str, str] = {}
    configs = {profile: _config_from_args(args, profile=profile) for profile in pending}
    from . import pipeline as pipeline_module

    multi = getattr(pipeline_module, "grade_universe_multi", None)
    if len(pending) > 1 and callable(multi):
        reports_by_profile = multi(snapshots, [configs[profile] for profile in pending])
    else:
        reports_by_profile = {
            profile: grade_universe(snapshots, configs[profile]) for profile in pending
        }
    for profile in pending:
        config = configs[profile]
        reports = reports_by_profile[profile]

        # A panel where (nearly) nothing is graded is a data outage — an EDGAR
        # blackout on freeze day makes every gate refuse — not a signal, and once a
        # valid-looking parquet exists it is trusted downstream forever. Refuse
        # below the same letter-floor peer minimum the grader itself enforces
        # (GradeConfig.min_letter_peers, default 15): fewer graded names than that
        # cannot even support a letter, let alone a cross-sectional panel. The
        # floor is per profile — a strict profile refusing on this universe says
        # nothing about the others, so it must not suppress them.
        graded_count = sum(1 for report in reports.values() if report.graded)
        if graded_count < config.min_letter_peers:
            console.print(
                f"[red]refusing to freeze {signal_date} for profile {profile}: only "
                f"{graded_count} of {len(reports)} scores are graded, below the "
                f"letter-floor minimum of {config.min_letter_peers}. This looks like a "
                f"data outage, not a cross-section; no panel was written to "
                f"{panel_path(profile)}.[/red]"
            )
            refused.append(profile)
            continue

        rows = [
            {
                "signal_date": signal_date.isoformat(),
                "ticker": report.ticker,
                "cik": report.meta.get("cik"),
                "score": report.score,
                "letter": report.letter,
                "percentile": report.percentile,
                "coverage": report.coverage,
                "graded": report.graded,
                "profile": report.profile,
                "config_fingerprint": report.meta.get("config_fingerprint"),
                "universe_fingerprint": report.meta.get("universe_fingerprint"),
                # Additive (schema stays 1.0): only a --pit freeze can honestly
                # claim its feature set closed at the signal date; without --pit
                # the SEC cache's contents at freeze time are the true bound and
                # writing signal_date would be an unearned attestation.
                "filed_through": (
                    signal_date.isoformat() if getattr(args, "pit", False) else None
                ),
                "universe_id": selection.universe_id,
                "universe_spec_sha256": selection.spec_sha256,
                "code_commit": commit,
                "schema_version": "1.0",
            }
            for report in reports.values()
        ]
        frame = pd.DataFrame(rows).sort_values("ticker")
        out_path = panel_path(profile)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False)
        tmp.replace(out_path)
        frozen_names[profile] = out_path.name
        console.print(
            f"froze {len(frame)} scores ({graded_count} graded) for {signal_date} -> {out_path}"
        )

    # The manifest IS the catalog: freezing a part and cataloging it are one
    # act. Refreshed for every requested profile directory — refused profiles
    # included — so a directory whose parts predate the convention is
    # backfilled the first time any freeze visits it.
    catalog_failures = _refresh_freeze_manifests(out_dir, profiles, frozen_names)

    if refused:
        # Alarm policy. Some profiles simply cannot grade this universe — momentum
        # and low_volatility need a denser daily price series than the free chain
        # supplies — so failing the run every month for a known, unchanging state
        # is how real alarms get trained away. Fail only on genuinely new
        # information: the profile the paper trader actually trades refusing, a
        # profile that HAS frozen before refusing now (a regression), or a month
        # where no requested profile has a same-date panel at all. Counting
        # already-frozen siblings makes unchanged structural refusals idempotent.
        regressed = sorted(p for p in refused if _has_prior_panel(out_dir, p, signal_date))
        has_same_date_output = any(panel_path(profile).is_file() for profile in profiles)
        console.print(
            f"[red]{len(refused)} of {len(pending)} profiles were refused: "
            f"{', '.join(refused)}[/red]"
        )
        if TRADED_PROFILE in refused or regressed or not has_same_date_output:
            if regressed:
                console.print(
                    f"[red]regression: {', '.join(regressed)} froze a panel before and "
                    f"refused now[/red]"
                )
            return 2
        console.print(
            "[yellow]those profiles have never frozen a panel on this universe "
            "(structural, not a regression); the run stays green[/yellow]"
        )
    # A part that could not be cataloged is genuinely new information — unlike
    # a structural refusal it can only mean corrupt bytes in the evidence tree.
    return 2 if catalog_failures else 0


def cmd_decay(args: argparse.Namespace) -> int:
    """Measure the score's rank-IC decay across holding horizons.

    A sweep is N extra looks at the same data: every horizon is charged as its
    own ledger trial on one shared denominator, and only the pre-declared
    primary horizon may pass the gate.
    """
    from .decay import (
        DecayConfig,
        decay_to_markdown,
        evaluate_decay,
        record_sweep_trials,
        write_decay_artifacts,
    )

    config = DecayConfig(
        horizons=tuple(args.horizons),
        primary_horizon=args.primary_horizon,
        quantiles=args.quantiles,
        min_cross_section=args.min_cross_section,
        transaction_cost_bps=args.transaction_cost_bps,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
        delisting_return=args.delisting_return,
        split_screen=args.split_screen,
        non_overlapping_only=args.non_overlapping,
    )
    if not args.allow_unverified_panel:
        console.print(
            "[red]decay panels fail 4 of 5 contract items by construction (surviving "
            "universe, price-only returns, no delisting proceeds, no filing cutoff); "
            "rerun with --allow-unverified-panel to acknowledge that[/red]"
        )
        return 2
    profiles = profile_names() if args.all_profiles else [args.profile]
    if args.all_profiles:
        status_console.print(
            f"[yellow]--all-profiles charges {len(config.horizons)} x {len(profiles)} "
            f"= {len(config.horizons) * len(profiles)} trials to the ledger[/yellow]"
        )
    exit_code = 0
    for profile in profiles:
        frozen_dir = Path(args.frozen_dir) / profile
        try:
            curve, panels = evaluate_decay(
                frozen_dir,
                args.vault,
                profile=profile,
                config=config,
                allow_fingerprint_drift=args.allow_fingerprint_drift,
                archive_through=args.archive_through,
            )
        except ValueError as exc:
            console.print(f"[red]{profile}: {exc}[/red]")
            exit_code = 2
            continue
        curve.ledger = record_sweep_trials(curve, ledger_path=Path(args.ledger))
        out_dir = write_decay_artifacts(curve, panels, args.out)
        status_console.print(
            f"[dim]{profile}: {curve.ledger['trials_added']} trial(s) recorded in "
            f"{args.ledger} (lifetime: {curve.ledger['lifetime_trials']}); artifacts "
            f"in {out_dir}[/dim]"
        )
        if args.format == "json":
            print(to_json(curve.to_dict()))
        elif args.format == "md":
            print(decay_to_markdown(curve))
        else:
            from rich.markdown import Markdown

            console.print(Markdown(decay_to_markdown(curve)))
    return exit_code


def cmd_build_panel(args: argparse.Namespace) -> int:
    """Join frozen panels to realized returns and emit a backtest-ready panel.

    Exit codes are the workflow contract — ``main()`` swallows exceptions into
    exit 1, so gates RETURN rather than raise:

    * 0 — built (or nothing matured yet, which is a structurally expected state
      for months after a fresh freeze; a red job for it would train the owner to
      ignore the failure email, the only alerting this ecosystem has).
    * 2 — refused: stale vault, dead freeze clock, backfilled signal dates, or
      too many unresolved rows.
    """
    from .data.vault import VaultDataSource
    from .panel import (
        PanelBuildConfig,
        PanelBuildError,
        archive_to_vault,
        build_panel,
        sidecar_payload,
        write_panel,
    )

    config = PanelBuildConfig(
        horizon_days=args.horizon_days,
        min_cross_section=args.min_cross_section,
        min_periods=args.min_periods,
        max_eod_lag_days=args.max_eod_lag_days,
        max_freeze_age_days=args.max_freeze_age_days,
        include_ungraded=args.include_ungraded,
        max_unresolved_fraction=args.max_unresolved_fraction,
        allow_backfilled_panels=args.allow_backfilled_panels,
    )
    vault = VaultDataSource(args.vault, verify_hashes=not args.no_verify_hashes)
    foundry = None
    if args.foundry:
        from .data.foundry import FoundryDataSource

        if args.foundry.startswith(("http://", "https://")):
            foundry = FoundryDataSource(url_base=args.foundry)
        else:
            foundry = FoundryDataSource(root=args.foundry)

    try:
        result = build_panel(Path(args.frozen_root), args.profile, vault, foundry, config)
    except PanelBuildError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    if result.refusal is not None:
        console.print(f"[red]refusing to build: {result.refusal}[/red]")
        return 2

    if not result.matured_signal_dates:
        _, sidecar = write_panel(result, Path(args.out), args.profile, config)
        if args.format == "json":
            print(to_json(sidecar_payload(result, args.profile, config)))
        else:
            console.print(
                "no frozen panel has matured yet (needs a signal date whose entry day "
                f"+ {config.horizon_days} trading days are archived); wrote {sidecar}"
            )
        return 0

    if result.unresolved_fraction > config.max_unresolved_fraction:
        console.print(
            f"[red]{result.unresolved_rows} unresolved row(s) "
            f"({result.unresolved_fraction:.1%}) exceed the "
            f"--max-unresolved-fraction budget of {config.max_unresolved_fraction:.1%}: "
            f"{', '.join(result.unresolved_tickers)}[/red]"
        )
        return 2

    panel_path, sidecar = write_panel(result, Path(args.out), args.profile, config)
    if panel_path is not None and args.archive_dir:
        archive_to_vault(
            panel_path, sidecar, Path(args.archive_dir), args.profile, date.today()
        )
    if args.format == "json":
        print(to_json(sidecar_payload(result, args.profile, config)))
    else:
        console.print(
            f"built {args.profile}: {sum(p.kept for p in result.periods)} rows across "
            f"{len(result.periods)} period(s), {result.qualifying_periods} qualifying, "
            f"ready_for_backtest={result.ready_for_backtest}, "
            f"dividend_coverage={result.dividend_coverage:.1%} "
            f"(return_is_total={result.attestations.get('return_is_total', False)})"
        )
        for p in result.periods:
            console.print(
                f"  {p.signal_date} [{p.return_start}..{p.return_end}] kept={p.kept} "
                f"ungraded={p.ungraded_dropped} no_start={p.no_start_price_dropped} "
                f"delisted={p.resolved_delisted_archive} terminal="
                f"{p.resolved_last_listed_close} splits="
                f"{p.split_adjusted_foundry + p.split_adjusted_reconstructed} "
                f"unresolved={p.unresolved_dropped}"
            )
    return 0


def cmd_build_signal_panel(args: argparse.Namespace) -> int:
    """Join Stock-Vault's raw signal observations to realized forward returns.

    The vault exports observations; this is the ONE place forward-return
    semantics live (splits, per-ex-date dividends, the delisting exit chain).
    The evaluable panel is written back INTO the private vault clone beside its
    observations — never here, and never printed: this repository is public and
    a per-row return derived from Massive closes must not reach it.

    Exit codes are the workflow contract:

    * 0 — built, or nothing to price yet (a structurally expected state for a
      signal whose first window has not closed).
    * 2 — refused: bad artifact, escaped path, or a symbology collision.
    """
    from .data.vault import VaultDataSource
    from .panel import PanelBuildError
    from .signal_panel import (
        SIGNAL_PANEL_VERSION,
        SignalPanelConfig,
        SignalPanelError,
        build_signal_panel,
        write_signal_panel,
    )

    vault = VaultDataSource(args.vault, verify_hashes=not args.no_verify_hashes)
    foundry = None
    if args.foundry:
        from .data.foundry import FoundryDataSource

        if args.foundry.startswith(("http://", "https://")):
            foundry = FoundryDataSource(url_base=args.foundry)
        else:
            foundry = FoundryDataSource(root=args.foundry)

    version = args.panel_version or SIGNAL_PANEL_VERSION
    available = vault.signal_panel_signals(version)
    if args.signal == "all":
        signals = available
    else:
        signals = [args.signal]
        if args.signal not in available:
            console.print(
                f"[red]{args.signal} has no v{version} observation dataset in "
                f"{args.vault} (available: {', '.join(available) or 'none'})[/red]"
            )
            return 2
    if not signals:
        console.print(
            f"no v{version} observation datasets under {args.vault}/data/signal_panels; "
            "run `stock-vault signal-panel` first"
        )
        return 0

    config = SignalPanelConfig(split_tolerance=args.split_tolerance, rebuild=args.rebuild)

    def _log(message: str) -> None:
        if args.verbose:
            console.print(f"  {message}")

    summaries = []
    failed = 0
    for signal in signals:
        try:
            result, new_parts = build_signal_panel(
                vault,
                signal,
                foundry=foundry,
                version=version,
                config=config,
                log=_log,
            )
            if result.refusal is not None:
                console.print(f"[red]{signal}: refusing to build — {result.refusal}[/red]")
                failed += 1
                continue
            write_signal_panel(vault, signal, result, new_parts, version=version)
        except (SignalPanelError, PanelBuildError) as exc:
            console.print(f"[red]{signal}: {exc}[/red]")
            failed += 1
            continue
        summaries.append(
            {
                "signal": signal,
                "panel_version": result.panel_version,
                "periods": result.observation_periods,
                "kept_rows": result.kept_rows,
                "unresolved_rows": result.unresolved_rows,
                "dividend_coverage": round(result.dividend_coverage, 4),
                "pit_membership_coverage": round(result.pit_membership_coverage, 4),
                "attestations": result.attestations,
                "last_run": {
                    "parts_written": result.parts_written,
                    "rows_written": result.rows_written,
                },
            }
        )
    if args.format == "json":
        print(to_json(summaries))
    else:
        for summary in summaries:
            console.print(
                f"{summary['signal']} v{summary['panel_version']}: "
                f"{summary['kept_rows']} row(s) over {summary['periods']} period(s), "
                f"wrote {summary['last_run']['parts_written']} part(s), "
                f"unresolved={summary['unresolved_rows']}, "
                f"dividend_coverage={summary['dividend_coverage']:.1%}, "
                f"pit_membership={summary['pit_membership_coverage']:.1%}, "
                f"attestations={summary['attestations']}"
            )
    return 2 if failed else 0


def cmd_check_cadence(args: argparse.Namespace) -> int:
    """Expectation clocks for the monthly evidence loop (see cadence.py).

    Exit 1 on a missed cadence — in a scheduled workflow the red run's failure
    email IS the alerting, exactly like the vault's check-staleness gates.
    Bootstrap-guarded: no artifacts at all passes with a note.
    """
    from .cadence import run_check

    return run_check(
        repo_root=args.repo_root,
        pre_run=args.pre_run,
        as_of=args.as_of,
        frozen_root=args.frozen_root,
        forward_dir=args.forward_dir,
    )


def cmd_forward_accounting(args: argparse.Namespace) -> int:
    """Record every profile's monthly forward state, matured or not.

    Written unconditionally by every monthly-forward-backtest run so 'the loop
    ran and nothing matured' is a recorded fact instead of silence. States are
    a closed vocabulary (no free-text refusal reasons), which is what keeps
    licensed derived numbers structurally unable to reach this public artifact.
    """
    from .cadence import run_account

    return run_account(
        month=args.month,
        states_path=args.states,
        out=args.out,
        event=args.event,
        run_id=args.run_id,
    )


def cmd_ledger_retract(args: argparse.Namespace) -> int:
    """Append a record that excludes earlier lines from trial accounting.

    The ledger is append-only and hash-chained, so a line that should never have
    counted as a trial cannot be deleted — deleting it is exactly the fraud the
    chain exists to expose. It is retracted by a later line that names it, and
    that line is itself hashed and chained, so the exclusion is as auditable as
    the thing it excludes.

    This exists because twelve of the live ledger's records are synthetic
    CLI-test panels (score=index, forward_return=index/1000) whose identical
    Sharpe of 2.83 would otherwise set the dispersion that every future real
    result is deflated against.
    """
    from .research_manifest import (
        RETRACTION_EXPERIMENT,
        ResearchRecord,
        append_record,
        current_commit,
        load_manifest,
        verify_chain,
    )

    ledger_path = Path(args.ledger)
    if not ledger_path.exists():
        console.print(f"[red]no ledger at {ledger_path}[/red]")
        return 2
    records = load_manifest(ledger_path)
    if not verify_chain(records):
        console.print(
            f"[red]{ledger_path} does not verify; refusing to append to a broken chain[/red]"
        )
        return 2
    known = {str(r.get("integrity_sha256", "")): r for r in records}
    requested = list(dict.fromkeys(args.sha256))
    missing = [h for h in requested if h not in known]
    if missing:
        console.print(f"[red]not in {ledger_path}: {', '.join(missing)}[/red]")
        return 2
    already = [h for h in requested if known[h].get("experiment") == RETRACTION_EXPERIMENT]
    if already:
        console.print(
            f"[red]refusing to retract a retraction record: {', '.join(already)}[/red]"
        )
        return 2
    record = ResearchRecord(
        experiment=RETRACTION_EXPERIMENT,
        market="us_equities",
        symbols=requested,
        targets=[],
        horizons=[],
        trials=0,
        metrics={},
        costs={},
        benchmark="none",
        leakage_controls="n/a",
        gate_passed=False,
        verdict=args.reason,
        code_commit=current_commit(),
    )
    append_record(ledger_path, record)
    console.print(f"retracted {len(requested)} record(s) in {ledger_path}: {args.reason}")
    return 0


def cmd_ledger_declare(args: argparse.Namespace) -> int:
    """Pre-register a backtest hypothesis BEFORE evaluating it.

    Appends a declaration record carrying the spec hash of the panel's
    identity plus the evaluation parameters, and the declared evaluation
    schedule. Later `backtest` runs whose observed spec matches are recorded
    as primary re-evaluations of this ONE trial instead of new trials, which
    is what stops a scheduled monthly re-evaluation from deflating one honest
    hypothesis as twenty-four.

    Idempotent: an identical spec already declared (and not retracted) appends
    nothing and exits 0, so a scheduled workflow can declare-if-absent on
    every run. Note what this does NOT do: the declared schedule makes the
    sequential looks disclosed, not statistically corrected — optional
    stopping is a different problem from trial-count inflation.
    """
    from .research_manifest import (
        append_record,
        find_preregistration,
        load_manifest,
        preregistered_experiment,
        preregistration_record,
        spec_sha256,
        verify_chain,
    )

    panel = _load_panel_frame(Path(args.panel))
    spec = _backtest_spec(panel, args)
    spec_hash = spec_sha256(spec)
    ledger_path = Path(args.ledger)
    records = load_manifest(ledger_path) if ledger_path.exists() else []
    if not verify_chain(records):
        console.print(
            f"[red]{ledger_path} does not verify; refusing to append to a broken chain[/red]"
        )
        return 2
    existing = find_preregistration(records, spec)
    if existing is not None:
        console.print(
            f"spec {spec_hash[:12]} already declared in {ledger_path} "
            f"({str(existing.get('integrity_sha256', ''))[:12]}); nothing to append"
        )
        return 0
    record = preregistration_record(spec, schedule=args.schedule)
    append_record(ledger_path, record)
    console.print(
        f"declared {preregistered_experiment(spec)} in {ledger_path} "
        f"(spec {spec_hash[:12]}, schedule: {args.schedule})"
    )
    return 0


def cmd_promotion_declare(args: argparse.Namespace) -> int:
    """Append a `ledger:promotion` record: a policy declaration or a stage
    transition under an already-declared policy.

    Policy mode (no --to-stage): binds the promotion-policy document's exact
    bytes by sha256 under a version string. Idempotent for an identical
    version+hash pair; a changed document under the SAME version is refused —
    amendment is a NEW version, and superseded declarations stay.

    Transition mode (--subject/--from-stage/--to-stage): validated against the
    policy AS DECLARED IN THE LEDGER, not against code constants — broken
    chain, undeclared policy, drifted policy doc, wrong current stage, skipped
    rungs, evidence-free promotions, and the unreachable live-money rung are
    all refusals. Evidence is cited as integrity hashes only: numeric evidence
    for license-walled vault signals stays in Stock-Vault's decision journal
    (see docs/PROMOTION.md, "The public/vault split").
    """
    from .research_manifest import (
        append_record,
        find_promotion_policy,
        load_manifest,
        promotion_policy_declaration,
        promotion_policy_record,
        promotion_transition_record,
        spec_sha256,
        validate_promotion_transition,
        verify_chain,
    )

    policy_doc = Path(args.policy_doc)
    if not policy_doc.exists():
        console.print(f"[red]no policy document at {policy_doc}[/red]")
        return 2
    doc_sha = hashlib.sha256(policy_doc.read_bytes()).hexdigest()
    ledger_path = Path(args.ledger)
    records = load_manifest(ledger_path) if ledger_path.exists() else []
    if not verify_chain(records):
        console.print(
            f"[red]{ledger_path} does not verify; refusing to append to a broken chain[/red]"
        )
        return 2

    transition_flags = [args.subject, args.from_stage, args.to_stage]
    if any(transition_flags) and not all(transition_flags):
        console.print(
            "[red]a transition needs --subject, --from-stage and --to-stage together[/red]"
        )
        return 2

    if not any(transition_flags):
        declared = find_promotion_policy(records, args.policy_version)
        if declared is not None:
            if str(declared.get("policy_sha256", "")) == doc_sha:
                console.print(
                    f"{args.policy_version} already declared in {ledger_path} "
                    f"(doc sha256 {doc_sha[:12]}); nothing to append"
                )
                return 0
            console.print(
                f"[red]{args.policy_version} is already declared with doc sha256 "
                f"{str(declared.get('policy_sha256', ''))[:12]} but {policy_doc} now "
                f"hashes to {doc_sha[:12]}; a changed policy is a NEW version, "
                f"never an in-place amendment[/red]"
            )
            return 2
        declaration = promotion_policy_declaration(
            policy_version=args.policy_version,
            policy_doc=str(policy_doc),
            policy_sha256=doc_sha,
            live_money_reachable=args.live_money_reachable,
        )
        record = promotion_policy_record(declaration)
        append_record(ledger_path, record)
        console.print(
            f"declared {args.policy_version} in {ledger_path} "
            f"(doc {policy_doc} sha256 {doc_sha[:12]}, "
            f"record {record.integrity_sha256()[:12]})"
        )
        return 0

    transition = {
        "kind": "stage-transition",
        "policy_version": args.policy_version,
        "policy_sha256": doc_sha,
        "subject": args.subject_label or args.subject[:12],
        "subject_spec_sha256": args.subject,
        "from_stage": args.from_stage,
        "to_stage": args.to_stage,
        "evidence_sha256": list(args.evidence or []),
        "evidence_journal": args.evidence_journal or "",
        "evidence_journal_head_sha256": args.evidence_journal_head or "",
        "reason": args.reason or "",
    }
    error = validate_promotion_transition(records, transition)
    if error is not None:
        console.print(f"[red]refused: {error}[/red]")
        return 2
    record = promotion_transition_record(transition)
    append_record(ledger_path, record)
    console.print(
        f"recorded {args.from_stage} -> {args.to_stage} for "
        f"{args.subject[:12]} under {args.policy_version} in {ledger_path} "
        f"(record {record.integrity_sha256()[:12]}, "
        f"transition sha256 {spec_sha256(transition)[:12]})"
    )
    return 0


def cmd_methods(args: argparse.Namespace) -> int:
    from rich.box import SIMPLE
    from rich.table import Table

    table = Table(box=SIMPLE, title="Weighting methods", title_justify="left", header_style="bold")
    table.add_column("name", style="cyan")
    table.add_column("panel?", justify="center")
    table.add_column("returns?", justify="center")
    table.add_column("fallback", style="dim")
    table.add_column("description")
    for name, info in sorted(WEIGHT_METHOD_INFO.items()):
        table.add_row(
            name,
            "yes" if info["needs_panel"] else "—",
            "yes" if info["needs_returns"] else "—",
            str(info["fallback"] or "—"),
            info["doc"][:70],
        )
    console.print(table)
    console.print(f"\n[dim]normalizers:[/dim] {', '.join(NORMALIZERS.names())}")
    console.print(f"[dim]aggregators:[/dim] {', '.join(AGGREGATORS.names())}")
    console.print(f"[dim]profiles:[/dim]    {', '.join(profile_names())}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    from rich.box import SIMPLE
    from rich.table import Table

    table = Table(
        box=SIMPLE,
        title=f"{len(METRICS)} registered metrics",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("metric", style="cyan")
    table.add_column("pillar")
    table.add_column("dir", justify="center")
    table.add_column("needs", style="dim")
    table.add_column("description")
    for name, spec in METRICS.items():
        if args.pillar and spec.pillar != args.pillar:
            continue
        direction = {1: "↑", -1: "↓", 0: "band"}.get(spec.direction, "?")
        needs = "prices" if spec.needs_prices else "fundamentals"
        if spec.needs_benchmark:
            needs += "+bench"
        table.add_row(name, spec.pillar, direction, needs, spec.description[:64])
    console.print(table)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-grader",
        description="Grade stocks from fundamentals, statistics and risk, with pluggable weighting.",
    )
    parser.add_argument("--version", action="version", version=f"stock-grader {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show warnings from data layers"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, needs_universe: bool = False) -> None:
        p.add_argument("--profile", default="all_weather", choices=profile_names())
        p.add_argument(
            "--weighting",
            choices=CLI_WEIGHTINGS,
            help="operational unsupervised weighting method at both levels",
        )
        p.add_argument("--normalizer", choices=CLI_NORMALIZERS)
        p.add_argument("--aggregator", choices=AGGREGATORS.names())
        p.add_argument("--rho", type=float, help="CES compensation parameter (1=mean, 0=geometric)")
        p.add_argument(
            "--curve",
            choices=["absolute", "cross_sectional", "hybrid"],
            help=(
                "score presentation; cross_sectional is the production default. absolute and "
                "hybrid are experimental peer-derived compatibility modes, not intrinsic values"
            ),
        )
        p.add_argument("--sector-neutral", action="store_true", help="score within sector")
        p.add_argument("--universe", required=needs_universe, help="file of tickers, one per line")
        p.add_argument(
            "--no-peers",
            action="store_true",
            help="skip the default peer universe (grade will carry no cross-sectional "
            "information and says so)",
        )
        p.add_argument("--asof", help="grade as of this date (YYYY-MM-DD)")
        p.add_argument(
            "--pit",
            action="store_true",
            help="point-in-time: use only figures filed on or before --asof",
        )
        p.add_argument(
            "--price",
            action="append",
            help="manual price, TICKER=123.45 (repeatable); enables valuation metrics "
            "with no market-data feed",
        )
        p.add_argument("--price-dir", help="directory of TICKER.csv price files")
        p.add_argument(
            "--vault",
            help="local Stock-Vault clone root; its verified EOD archive leads the price chain",
        )
        p.add_argument(
            "--price-provider",
            default="auto",
            choices=["auto", "csv", "tiingo", "stockanalysis", "yahoo", "sec", "none"],
            help=(
                "daily-price source (default auto: CSV when supplied, optional StockAnalysis, "
                "then Tiingo when configured, then Yahoo). 'sec' uses only sparse SEC-derived "
                "scalar prices; 'none' disables the daily-price chain"
            ),
        )
        p.add_argument(
            "--synthetic-prices",
            action="store_true",
            help="fabricate a price series where none is available; clearly labelled "
            "in the report and never real market history",
        )
        p.add_argument(
            "--sec-prices",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="derive prices from SEC insider-transaction filings (default: on) — "
            "the only keyless price source, sparse but real",
        )
        p.add_argument(
            "--max-price-age",
            type=int,
            default=400,
            help="refuse any SEC-derived price older than this many days (default 400)",
        )
        p.add_argument(
            "--stockanalysis",
            action="store_true",
            help="opt-in StockAnalysis in the auto chain (equivalent to "
            "--price-provider stockanalysis when used alone). It is an undocumented "
            "commercial endpoint, not a licensed feed — read their ToS first",
        )
        p.add_argument(
            "--benchmark",
            default="SP500",
            help="FRED index for beta/alpha (SP500, NASDAQ, DJIA); price-only, so alpha "
            "is overstated by roughly beta x the index dividend yield",
        )
        p.add_argument("--no-network", action="store_true", help="SEC cache only, no price fetches")
        p.add_argument(
            "--foundry",
            help="Stock-Data foundry location (local clone path or raw URL): supplies "
            "reconstructed dividends-per-share as a fallback for dividend metrics",
        )
        p.add_argument("--refresh", action="store_true", help="bypass the cache")
        # expanduser at the boundary: a POSIX shell only expands a bare leading
        # ~, so --cache-dir=~/.cache/stock-grader arrives literal and two of the
        # consumers below expanded it while the rest made a directory named "~".
        p.add_argument("--cache-dir", type=lambda value: str(Path(value).expanduser()))
        p.add_argument("--contact", help="contact address sent to SEC in the User-Agent")
        p.add_argument(
            "--bulk-facts",
            choices=["auto", "always", "never"],
            default="auto",
            help="SEC bulk Companyfacts policy (auto above 200 tickers when online)",
        )
        p.add_argument("--format", default="text", choices=["text", "json", "md"])

    p_grade = sub.add_parser("grade", help="grade one or more securities")
    p_grade.add_argument("tickers", nargs="+")
    p_grade.add_argument("--explain", action="store_true", help="show per-metric drivers")
    p_grade.add_argument(
        "--journal-dir",
        type=lambda value: str(Path(value).expanduser()),
        help="run-journal directory (default ~/.stock-grader/runs); every grade run "
        "appends its reports there, which is what feeds letter hysteresis and "
        "`stock-grader diff --since-last`",
    )
    p_grade.add_argument(
        "--no-journal",
        action="store_true",
        help="neither read nor append the run journal; letter hysteresis needs the "
        "previous run's letters, so this also disables it",
    )
    p_grade.add_argument(
        "--hysteresis",
        action="store_true",
        help="keep a letter stable when the score moved only trivially, using the "
        "previous comparable journaled run's letters as the prior state "
        "(off by default per design decision D9: prior state is an explicit input)",
    )
    common(p_grade)
    p_grade.set_defaults(func=cmd_grade)

    p_diff = sub.add_parser(
        "diff",
        help="compare a ticker's newest journaled grade against the run before it",
    )
    p_diff.add_argument("ticker")
    p_diff.add_argument(
        "--since-last",
        action="store_true",
        required=True,
        help="baseline = the most recent earlier journaled run containing TICKER "
        "(the only baseline mode today; explicit so future modes stay additive)",
    )
    p_diff.add_argument(
        "--journal-dir",
        type=lambda value: str(Path(value).expanduser()),
        help="run-journal directory (default ~/.stock-grader/runs)",
    )
    p_diff.add_argument(
        "--allow-fingerprint-drift",
        action="store_true",
        help="accept a config or peer-set regime break between the two runs "
        "(reported in the output rather than hidden)",
    )
    p_diff.add_argument("--format", default="text", choices=["text", "json"])
    p_diff.set_defaults(func=cmd_diff)

    p_rank = sub.add_parser("rank", help="rank a universe")
    p_rank.add_argument("--top", type=_positive_int)
    common(p_rank, needs_universe=True)
    p_rank.set_defaults(func=cmd_rank)

    p_freeze = sub.add_parser(
        "freeze",
        help="freeze today's scores into the append-only forward panel (never overwrites)",
    )
    p_freeze.add_argument(
        "--out",
        default="frozen_scores",
        help="directory of immutable <profile>/<date>.parquet files",
    )
    p_freeze.add_argument(
        "--all-profiles",
        action="store_true",
        help="grade the one set of snapshots under every registered profile "
        "and write a panel for each (no extra network calls); --profile "
        "is ignored",
    )
    common(p_freeze, needs_universe=True)
    p_freeze.set_defaults(func=cmd_freeze)

    p_cons = sub.add_parser("consensus", help="grade under every profile and report disagreement")
    p_cons.add_argument("tickers", nargs="+")
    common(p_cons)
    p_cons.set_defaults(func=cmd_consensus)

    p_research = sub.add_parser(
        "research",
        help="build an auditable peer, trend, metric, and scenario-valuation dossier",
    )
    p_research.add_argument("ticker")
    p_research.add_argument(
        "--peer-mode",
        choices=["auto", "explicit"],
        default="auto",
        help="auto selects by SIC/business model/size; explicit retains --universe members",
    )
    p_research.add_argument("--peer-min", type=_positive_int, default=8)
    p_research.add_argument("--peer-max", type=_positive_int, default=30)
    p_research.add_argument(
        "--size-band",
        type=float,
        default=5.0,
        help="prefer peers within this market-cap multiple (default 5)",
    )
    p_research.add_argument(
        "--dcf-growth",
        nargs=3,
        type=float,
        metavar=("BEAR", "BASE", "BULL"),
        default=(-0.02, 0.05, 0.12),
        help="illustrative annual cash-flow growth assumptions as decimals",
    )
    p_research.add_argument(
        "--discount-rate",
        type=float,
        default=None,
        help="explicit required equity return; omit to derive risk-free + equity risk premium",
    )
    p_research.add_argument("--terminal-growth", type=float, default=0.025)
    common(p_research)
    p_research.set_defaults(func=cmd_research)

    p_backtest = sub.add_parser(
        "backtest",
        help="evaluate a frozen point-in-time score panel against later total returns",
    )
    p_backtest.add_argument("panel", help="CSV or Parquet score panel")
    p_backtest.add_argument("--quantiles", type=_positive_int, default=5)
    p_backtest.add_argument("--min-cross-section", type=_positive_int, default=20)
    p_backtest.add_argument("--periods-per-year", type=_positive_int, default=12)
    p_backtest.add_argument("--transaction-cost-bps", type=float, default=10.0)
    p_backtest.add_argument("--bootstrap-samples", type=int, default=1_000)
    p_backtest.add_argument("--bootstrap-block-periods", type=_positive_int, default=3)
    p_backtest.add_argument("--seed", type=int, default=0)
    p_backtest.add_argument(
        "--ledger",
        default="research_ledger.jsonl",
        help="append-only trial ledger; the deflated-Sharpe "
        "correction counts every trial ever recorded here",
    )
    p_backtest.add_argument(
        "--allow-unverified-panel",
        action="store_true",
        help=(
            "run despite missing PIT-universe, total-return, delisting, filing-cutoff, or "
            "permanent-identifier evidence; the report will retain those caveats"
        ),
    )
    p_backtest.add_argument(
        "--allow-mixed-universes",
        action="store_true",
        help="explicitly allow panels containing more than one universe_id",
    )
    p_backtest.add_argument("--format", default="text", choices=["text", "json", "md"])
    p_backtest.set_defaults(func=cmd_backtest)

    p_methods = sub.add_parser("methods", help="list weighting methods, normalizers, aggregators")
    p_methods.set_defaults(func=cmd_methods)

    p_metrics = sub.add_parser("metrics", help="list registered metrics")
    p_metrics.add_argument("--pillar")
    p_metrics.set_defaults(func=cmd_metrics)

    p_build = sub.add_parser(
        "build-panel",
        help="join frozen panels to realized returns for the backtest evaluator",
    )
    p_build.add_argument("--profile", required=True, choices=profile_names())
    p_build.add_argument("--frozen-root", default="frozen_scores")
    p_build.add_argument(
        "--vault",
        required=True,
        help="local Stock-Vault clone (the source is local-clone-only by design)",
    )
    p_build.add_argument("--foundry", help="Stock-Data clone or raw URL, for split confirmation")
    p_build.add_argument("--out", default="build/panels")
    p_build.add_argument(
        "--archive-dir", help="when set, copy the panel into this vault dataset root"
    )
    p_build.add_argument("--horizon-days", type=_positive_int, default=21)
    p_build.add_argument("--min-cross-section", type=_positive_int, default=20)
    p_build.add_argument("--min-periods", type=_positive_int, default=3)
    p_build.add_argument("--max-eod-lag-days", type=_positive_int, default=5)
    p_build.add_argument("--max-freeze-age-days", type=_positive_int, default=45)
    p_build.add_argument("--max-unresolved-fraction", type=float, default=0.02)
    p_build.add_argument("--include-ungraded", action="store_true")
    p_build.add_argument("--allow-backfilled-panels", action="store_true")
    p_build.add_argument("--no-verify-hashes", action="store_true")
    p_build.add_argument("--format", choices=("text", "json"), default="text")
    p_build.set_defaults(func=cmd_build_panel)

    p_signal = sub.add_parser(
        "build-signal-panel",
        help="join Stock-Vault's raw signal observations to realized forward returns "
        "(the single owner of forward-return semantics); writes into the vault",
    )
    p_signal.add_argument(
        "--signal", default="all", help="signal name, or 'all' (default) for every dataset"
    )
    p_signal.add_argument(
        "--vault",
        required=True,
        help="local Stock-Vault clone; the evaluable panel is written INSIDE it "
        "(licensed per-row returns never reach this public repo)",
    )
    p_signal.add_argument(
        "--foundry", help="Stock-Data clone or raw URL, for split confirmation"
    )
    p_signal.add_argument(
        "--panel-version",
        type=_positive_int,
        default=None,
        help="vault panel layout to read and write (default: the builder's own)",
    )
    p_signal.add_argument("--split-tolerance", type=float, default=0.01)
    p_signal.add_argument(
        "--rebuild",
        action="store_true",
        help="re-price signal dates whose part already exists (parts are immutable "
        "by default)",
    )
    p_signal.add_argument("--no-verify-hashes", action="store_true")
    p_signal.add_argument("--verbose", action="store_true")
    p_signal.add_argument("--format", choices=("text", "json"), default="text")
    p_signal.set_defaults(func=cmd_build_signal_panel)

    p_decay = sub.add_parser(
        "decay",
        help="measure the score's rank-IC decay across holding horizons (5/21/63/126d)",
    )
    p_decay.add_argument(
        "--frozen-dir",
        default="frozen_scores",
        help="root of the frozen panels; the profile subdirectory is appended",
    )
    p_decay.add_argument(
        "--vault", required=True, help="local Stock-Vault clone (private, local-only)"
    )
    p_decay.add_argument("--profile", default="all_weather", choices=profile_names())
    p_decay.add_argument(
        "--all-profiles",
        action="store_true",
        help="sweep every profile; charges len(horizons) x 11 trials to the ledger",
    )
    p_decay.add_argument("--horizons", nargs="+", type=_positive_int, default=[5, 21, 63, 126])
    p_decay.add_argument(
        "--primary-horizon",
        type=_positive_int,
        default=21,
        help="the ONE pre-declared horizon allowed to pass the gate",
    )
    p_decay.add_argument("--out", default="signal_decay")
    p_decay.add_argument("--quantiles", type=_positive_int, default=5)
    p_decay.add_argument("--min-cross-section", type=_positive_int, default=20)
    p_decay.add_argument("--transaction-cost-bps", type=float, default=10.0)
    p_decay.add_argument("--bootstrap-samples", type=int, default=1_000)
    p_decay.add_argument("--seed", type=int, default=0)
    p_decay.add_argument(
        "--delisting-return",
        type=float,
        default=None,
        help="Shumway-style imputation for names that stop trading mid-window; "
        "default drops and counts them",
    )
    p_decay.add_argument(
        "--no-split-screen", dest="split_screen", action="store_false", default=True
    )
    p_decay.add_argument("--non-overlapping", action="store_true")
    p_decay.add_argument(
        "--archive-through",
        default=None,
        help="ignore vault sessions after this ISO date (NOT --asof: main() reserves it)",
    )
    p_decay.add_argument("--allow-fingerprint-drift", action="store_true")
    p_decay.add_argument("--allow-unverified-panel", action="store_true")
    p_decay.add_argument("--ledger", default="research_ledger.jsonl")
    p_decay.add_argument("--format", default="text", choices=["text", "json", "md"])
    p_decay.set_defaults(func=cmd_decay)

    p_declare = sub.add_parser(
        "ledger-declare",
        help="pre-register a backtest panel spec so scheduled re-evaluations "
        "count as one trial, not one per look",
    )
    p_declare.add_argument("panel", help="CSV or Parquet score panel the spec is read from")
    # Evaluation parameters are part of the hypothesis identity, so they mirror
    # the `backtest` subcommand's names AND defaults exactly — a declaration
    # made with defaults must match a backtest run with defaults.
    p_declare.add_argument("--quantiles", type=_positive_int, default=5)
    p_declare.add_argument("--min-cross-section", type=_positive_int, default=20)
    p_declare.add_argument("--periods-per-year", type=_positive_int, default=12)
    p_declare.add_argument("--transaction-cost-bps", type=float, default=10.0)
    p_declare.add_argument("--ledger", default="research_ledger.jsonl")
    p_declare.add_argument(
        "--schedule",
        required=True,
        help='declared evaluation schedule, recorded verbatim in the declaration, e.g. '
        '"monthly (cron 41 2 6 * *)" — sequential looks are disclosed, not corrected',
    )
    p_declare.set_defaults(func=cmd_ledger_declare)

    p_retract = sub.add_parser(
        "ledger-retract",
        help="append a record retracting earlier ledger lines from trial accounting",
    )
    p_retract.add_argument(
        "sha256",
        nargs="+",
        help="integrity_sha256 of each record to retract",
    )
    p_retract.add_argument("--ledger", default="research_ledger.jsonl")
    p_retract.add_argument(
        "--reason",
        required=True,
        help="why these records are not hypotheses; recorded verbatim as the verdict",
    )
    p_retract.set_defaults(func=cmd_ledger_retract)

    # Shared ledger options as an argparse parent: parents= must ride on the
    # SUBPARSER (add_parser), never the top-level parser, or the option lands
    # before the command word.
    shared_ledger = argparse.ArgumentParser(add_help=False)
    shared_ledger.add_argument(
        "--ledger",
        default="research_ledger.jsonl",
        help="append-only hash-chained research ledger",
    )
    p_promote = sub.add_parser(
        "promotion-declare",
        parents=[shared_ledger],
        help="append a ledger:promotion record — declare the versioned promotion "
        "policy, or one stage transition under it (docs/PROMOTION.md)",
    )
    p_promote.add_argument(
        "--policy-doc",
        default="docs/PROMOTION.md",
        help="the policy document whose exact bytes the declaration binds by sha256",
    )
    p_promote.add_argument(
        "--policy-version",
        required=True,
        help='version string, e.g. "promotion-policy-v1"; amendments are a NEW version',
    )
    p_promote.add_argument(
        "--live-money-reachable",
        action="store_true",
        help="policy mode only: declare the live-money rung reachable "
        "(v1 deliberately does NOT pass this)",
    )
    p_promote.add_argument(
        "--subject",
        help="transition mode: the promoted spec's sha256 (the subject's identity)",
    )
    p_promote.add_argument(
        "--subject-label",
        help="human-readable subject name for the verdict line (a name, never a result)",
    )
    p_promote.add_argument(
        "--from-stage", help="transition mode: the subject's current recorded stage"
    )
    p_promote.add_argument("--to-stage", help="transition mode: the stage moved to")
    p_promote.add_argument(
        "--evidence",
        nargs="*",
        help="integrity sha256 of each evidence record the decision rests on "
        "(ledger lines here, or Stock-Vault decision-journal records)",
    )
    p_promote.add_argument(
        "--evidence-journal",
        help='locator of the private evidence journal, e.g. '
        '"Stock-Vault data/decision_journal/decisions.jsonl.gz"',
    )
    p_promote.add_argument(
        "--evidence-journal-head",
        help="chain-head sha256 of the private evidence journal at decision time",
    )
    p_promote.add_argument(
        "--reason",
        help="why this transition is being recorded; recorded verbatim in the verdict",
    )
    p_promote.set_defaults(func=cmd_promotion_declare)

    p_cadence = sub.add_parser(
        "check-cadence",
        help="verify the evidence loop's monthly expectation clocks "
        "(forward accounting + freeze); exit 1 on a missed cadence",
    )
    p_cadence.add_argument("--repo-root", default=".", help="Stock-Grader checkout root")
    p_cadence.add_argument(
        "--pre-run",
        action="store_true",
        help="self-gate mode for monthly-forward-backtest itself: the gated run "
        "writes the current month's accounting, so hold the accounting clock "
        "to the previous month",
    )
    p_cadence.add_argument(
        "--as-of", default=None, help="ISO date to evaluate at (default: today UTC)"
    )
    p_cadence.add_argument("--frozen-root", default="frozen_scores")
    p_cadence.add_argument("--forward-dir", default="docs/forward")
    p_cadence.set_defaults(func=cmd_check_cadence)

    p_account = sub.add_parser(
        "forward-accounting",
        help="append this run's per-profile evaluated/not-matured/refused states "
        "to docs/forward/<YYYY-MM>/accounting.json",
    )
    p_account.add_argument("--month", required=True, help="YYYY-MM being accounted")
    p_account.add_argument(
        "--states", required=True, help="TSV file of '<profile>\\t<state>' lines"
    )
    p_account.add_argument("--out", default="docs/forward")
    p_account.add_argument("--event", default="manual", help="what triggered the run")
    p_account.add_argument("--run-id", default=None, help="workflow run id, if any")
    p_account.set_defaults(func=cmd_forward_accounting)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_network", False) and getattr(args, "refresh", False):
        parser.error("--refresh cannot be combined with --no-network")
    if getattr(args, "asof", None):
        try:
            requested_asof = date.fromisoformat(args.asof)
        except ValueError:
            parser.error("--asof must be an ISO date (YYYY-MM-DD)")
        if requested_asof != date.today() and not getattr(args, "pit", False):
            parser.error("historical --asof requires --pit")
    if getattr(args, "command", None) == "research":
        if args.peer_mode == "explicit" and not args.universe:
            parser.error("--peer-mode explicit requires --universe")
        if args.peer_max < args.peer_min:
            parser.error("--peer-max must be greater than or equal to --peer-min")
        if not math.isfinite(args.size_band) or args.size_band <= 1.0:
            parser.error("--size-band must be finite and greater than 1")
        if not all(math.isfinite(value) for value in args.dcf_growth):
            parser.error("--dcf-growth values must be finite")
        if any(value <= -1.0 for value in args.dcf_growth):
            parser.error("--dcf-growth values must be greater than -1")
        if not math.isfinite(args.terminal_growth) or args.terminal_growth <= -1.0:
            parser.error("--terminal-growth must be a finite rate greater than -1")
        if args.discount_rate is not None:
            if not math.isfinite(args.discount_rate) or args.discount_rate <= -1.0:
                parser.error("--discount-rate must be a finite rate greater than -1")
            if args.discount_rate <= args.terminal_growth:
                parser.error("--discount-rate must be greater than --terminal-growth")
    if getattr(args, "rho", None) is not None and getattr(args, "aggregator", None) not in (
        None,
        "ces",
    ):
        parser.error("--rho applies only when the pillar aggregator is ces")
    if getattr(args, "rho", None) is not None and (
        not math.isfinite(args.rho) or not -20.0 <= args.rho <= 20.0
    ):
        parser.error("--rho must be finite and in [-20, 20]")
    if getattr(args, "command", None) == "backtest":
        if args.quantiles < 2:
            parser.error("--quantiles must be at least 2")
        if args.min_cross_section < args.quantiles * 2:
            parser.error("--min-cross-section must be at least twice --quantiles")
        if not math.isfinite(args.transaction_cost_bps) or args.transaction_cost_bps < 0:
            parser.error("--transaction-cost-bps must be finite and non-negative")
        if args.bootstrap_samples < 0:
            parser.error("--bootstrap-samples must be non-negative")
    if getattr(args, "command", None) == "decay":
        if args.quantiles < 2:
            parser.error("--quantiles must be at least 2")
        if args.min_cross_section < args.quantiles * 2:
            parser.error("--min-cross-section must be at least twice --quantiles")
        if not math.isfinite(args.transaction_cost_bps) or args.transaction_cost_bps < 0:
            parser.error("--transaction-cost-bps must be finite and non-negative")
        if args.bootstrap_samples < 0:
            parser.error("--bootstrap-samples must be non-negative")
        horizons = list(args.horizons)
        if horizons != sorted(set(horizons)) or any(h > 504 for h in horizons):
            parser.error("--horizons must be strictly increasing, unique, and <= 504")
        if args.primary_horizon not in horizons:
            parser.error(
                "--primary-horizon must be one of --horizons: declare which horizon "
                "you are testing BEFORE you look at the others"
            )
        if args.delisting_return is not None and not (
            math.isfinite(args.delisting_return) and -1.0 <= args.delisting_return <= 0.0
        ):
            parser.error("--delisting-return must be finite and in [-1, 0]")
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.ERROR,
        format="[%(levelname)s] %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        return 130
    except Exception as exc:
        console.print(f"[red]error:[/red] {exc}")
        if getattr(args, "verbose", False):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
