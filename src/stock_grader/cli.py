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
from .metrics import fundamental, models, sector_specific, statistical  # noqa: F401
from .peers import explicit_peers, select_peers
from .pipeline import GradeConfig, grade_universe
from .profiles import consensus_grade, get_profile, profile_names
from .registry import AGGREGATORS, METRICS, NORMALIZERS, WEIGHTINGS
from .report import (
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
    bulk_cache = Path(args.cache_dir).expanduser().resolve() / "bulk" if args.cache_dir else None
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
            Path(args.cache_dir).expanduser().resolve() / "vault" if args.cache_dir else None
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
            status_console.print(f"[yellow]foundry unavailable: {exc}[/yellow]")
            foundry = None

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
            try:
                dps = foundry.trailing_dps(ticker)
            except Exception:
                dps = None
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


def cmd_grade(args: argparse.Namespace) -> int:
    tickers = [t.upper() for t in args.tickers]
    peers = _resolve_peers(args, tickers)
    all_tickers = list(dict.fromkeys(tickers + peers))
    provider = _sec_provider_from_args(args, len(all_tickers))
    snapshots = _build_snapshots(all_tickers, args, provider=provider)
    if not snapshots:
        console.print("[red]no securities could be loaded[/red]")
        return 2

    reports = grade_universe(snapshots, _config_from_args(args))
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
    return 0 if any(report.graded for report in selected.values()) else 3


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


def cmd_backtest(args: argparse.Namespace) -> int:
    """Evaluate a caller-supplied, frozen point-in-time score panel."""

    path = Path(args.panel)
    if not path.exists():
        raise ValueError(f"panel does not exist: {path}")
    if path.suffix.lower() in {".parquet", ".pq"}:
        panel = pd.read_parquet(path)
    else:
        panel = pd.read_csv(path)
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
        load_manifest,
    )
    from .significance import assess_edge, per_period_sharpe

    net_spreads = [p.net_spread for p in report.periods]
    ledger_path = Path(getattr(args, "ledger", "research_ledger.jsonl"))
    prior = load_manifest(ledger_path) if ledger_path.exists() else []
    # Only finite trial Sharpes deflate: a null/NaN sharpe (a panel too short to
    # compute one) is a trial with no usable statistic, and letting it through
    # makes stdev(trial_sharpes) NaN and therefore DSR=NaN on every future run.
    trial_sharpes = [
        record["metrics"]["per_period_sharpe"]
        for record in prior
        if isinstance(record.get("metrics"), dict)
        and isinstance(record["metrics"].get("per_period_sharpe"), (int, float))
        and math.isfinite(record["metrics"]["per_period_sharpe"])
    ]
    this_sharpe = per_period_sharpe(net_spreads) if len(net_spreads) >= 2 else float("nan")
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
    append_record(
        ledger_path,
        ResearchRecord(
            experiment=f"backtest:{path.name}",
            market="us_equities",
            symbols=[],
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
            ),
            gate_passed=bool(significance and significance.significant),
            verdict=significance.verdict if significance else "insufficient periods",
            data_span=(
                f"{report.periods[0].signal_date}..{report.periods[-1].signal_date}"
                if report.periods
                else ""
            ),
            code_commit=current_commit(),
        ),
    )
    status_console.print(
        f"[dim]trial recorded in {ledger_path} (lifetime trials: {len(trial_sharpes)})[/dim]"
    )

    if args.format == "json":
        # Additive keys on the documented report schema - never an envelope.
        payload = json.loads(to_json(report))
        if significance is not None:
            payload["significance"] = json.loads(to_json(significance))
        payload["ledger"] = {"path": str(ledger_path), "lifetime_trials": len(trial_sharpes)}
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
        return 0

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
        console.print(
            f"froze {len(frame)} scores ({graded_count} graded) for {signal_date} -> {out_path}"
        )

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
        p.add_argument("--cache-dir")
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
    common(p_grade)
    p_grade.set_defaults(func=cmd_grade)

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
