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
import logging
import sys
from datetime import date
from pathlib import Path

from rich.console import Console

from . import __version__

# Importing these modules is what populates the registries.
from . import aggregate, normalize, weighting  # noqa: F401
from .metrics import fundamental, models, statistical  # noqa: F401

from .data.prices import (
    BenchmarkProvider,
    ChainedPriceProvider,
    CSVPriceProvider,
    RiskFreeProvider,
    YahooPriceProvider,
)
from .data.sec import SECClient, SECProvider
from .data.stockanalysis import StockAnalysisPriceProvider
from .data.sec_prices import SECInsiderPriceProvider, resolve_price
from .data.synthetic import generate_prices
from .pipeline import GradeConfig, grade_universe
from .profiles import consensus_grade, get_profile, profile_names
from .registry import AGGREGATORS, METRICS, NORMALIZERS, WEIGHTINGS
from .report import render_consensus, render_ranking, render_report, to_json, to_markdown
from .types import PitMode, SecuritySnapshot
from .weighting import WEIGHT_METHOD_INFO

console = Console()


def _default_universe_path() -> Path | None:
    """Locate the bundled default peer list, whether running from a checkout or an install."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "config" / "universe_default.txt",
        Path(__file__).resolve().parent / "config" / "universe_default.txt",
        Path.cwd() / "config" / "universe_default.txt",
    ]
    return next((p for p in candidates if p.exists()), None)


def _load_universe(path: str) -> list[str]:
    text = Path(path).read_text()
    tickers: list[str] = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        tickers.extend(part.strip().upper() for part in line.replace(",", " ").split() if part.strip())
    return tickers


def _resolve_peers(args: argparse.Namespace, tickers: list[str]) -> list[str]:
    """Decide what to compare against.

    A cross-sectional grade is meaningless without a peer group, so an explicit ``--universe`` wins,
    the bundled default list is the fallback, and ``--no-peers`` opts out with the consequence
    stated plainly in the report rather than silently producing a flat-50 grade.
    """
    if args.universe:
        return _load_universe(args.universe)
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
        console.print("[yellow]no default universe found; grading without peers[/yellow]")
        return []
    peers = _load_universe(str(path))
    console.print(
        f"[dim]comparing against {len(peers)} default peers "
        f"(--universe FILE to choose your own, --no-peers to skip)[/dim]"
    )
    return peers


def _build_snapshots(
    tickers: list[str],
    args: argparse.Namespace,
    *,
    provider: SECProvider,
) -> list[SecuritySnapshot]:
    """Fetch fundamentals for each ticker and attach prices where available."""
    price_providers = []
    if args.price_dir:
        price_providers.append(CSVPriceProvider(args.price_dir))
    if args.stockanalysis and not args.no_network:
        # Opt-in: an undocumented endpoint of a commercial site. See that module's docstring.
        price_providers.append(
            StockAnalysisPriceProvider(cache_dir=args.cache_dir, contact=args.contact)
        )
    if not args.no_network:
        price_providers.append(YahooPriceProvider())
    prices = ChainedPriceProvider(price_providers) if price_providers else None

    risk_free = None
    benchmark = None
    if not args.no_network:
        risk_free = RiskFreeProvider().get("3m")
        # Without this, beta / capm_alpha / idiosyncratic_volatility can never fire at all.
        benchmark = BenchmarkProvider(cache_dir=args.cache_dir).get(args.benchmark)

    # SEC insider-transaction prices: the only price source reachable without an API key.
    # Sparse (a few dates per quarter), which is enough for valuation but not for the daily
    # statistics, so it sets `price` and deliberately leaves `prices` unset.
    insider = None
    if args.sec_prices and not args.no_network:
        insider = SECInsiderPriceProvider(cache_dir=args.cache_dir, contact=args.contact)
        with console.status("[dim]loading SEC insider-transaction prices…[/dim]"):
            insider.load(asof=date.fromisoformat(args.asof) if args.asof else None)
        console.print(f"[dim]SEC insider prices: {insider.coverage()} tickers[/dim]")

    manual_prices = {}
    for entry in args.price or []:
        if "=" in entry:
            ticker, value = entry.split("=", 1)
            manual_prices[ticker.strip().upper()] = float(value)
        elif len(tickers) == 1:
            manual_prices[tickers[0]] = float(entry)

    asof = date.fromisoformat(args.asof) if args.asof else date.today()
    pit_mode = PitMode.PIT if args.pit else PitMode.LATEST

    snapshots: list[SecuritySnapshot] = []
    status = console.status("[dim]loading securities…[/dim]") if len(tickers) > 1 else None
    if status:
        status.start()
    for i, ticker in enumerate(tickers, 1):
        if status:
            status.update(f"[dim]loading {ticker} ({i}/{len(tickers)})…[/dim]")
        snapshot = provider.fetch(ticker, asof=asof, pit_mode=pit_mode, refresh=args.refresh)
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
        if args.synthetic_prices and snapshot.prices is None:
            snapshot.prices = generate_prices(ticker, n_days=1300, end=asof, synthetic=True)
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
                snapshot.price = found["price"]
                snapshot.meta["price_source"] = found["source"]
                snapshot.meta["price_date"] = found["date"].isoformat()
                snapshot.meta["price_age_days"] = found["age_days"]
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
                        "price implied from SEC public float with no affiliate correction — a "
                        "LOWER bound, so valuation may make this look cheaper than it is"
                    )
        if ticker in manual_prices:
            snapshot.price = manual_prices[ticker]
            snapshot.meta["price_source"] = "manual"
        snapshot.risk_free = risk_free
        if benchmark is not None:
            snapshot.benchmark = benchmark
            snapshot.meta["benchmark"] = args.benchmark
            # A price index excludes dividends, so alpha against it is overstated by roughly
            # beta x the index dividend yield.
            snapshot.meta["benchmark_is_price_only"] = True
        snapshots.append(snapshot)
    if status:
        status.stop()

    unresolved = [s.ticker for s in snapshots if s.cik is None]
    if unresolved:
        # One aggregate note rather than N scrolling warnings.
        console.print(
            f"[yellow]{len(unresolved)}/{len(snapshots)} tickers are absent from SEC's ticker "
            f"map ({', '.join(unresolved[:6])}{'…' if len(unresolved) > 6 else ''}). That map "
            f"lists only currently-listed issuers, so delisted companies are missing and reused "
            f"tickers resolve to the survivor — BBBY resolves to the entity that bought the brand, "
            f"not the retailer that failed.[/yellow]"
        )
    return snapshots


def _config_from_args(args: argparse.Namespace) -> GradeConfig:
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
    return get_profile(args.profile, **overrides)


def cmd_grade(args: argparse.Namespace) -> int:
    provider = SECProvider(SECClient(cache_dir=args.cache_dir, contact=args.contact))
    tickers = [t.upper() for t in args.tickers]
    peers = _resolve_peers(args, tickers)
    all_tickers = list(dict.fromkeys(tickers + peers))
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
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    provider = SECProvider(SECClient(cache_dir=args.cache_dir, contact=args.contact))
    tickers = _load_universe(args.universe)
    if not tickers:
        console.print("[red]universe file is empty[/red]")
        return 2
    snapshots = _build_snapshots(tickers, args, provider=provider)
    reports = grade_universe(snapshots, _config_from_args(args))
    if args.format == "json":
        print(to_json(reports))
    else:
        render_ranking(reports, console, top=args.top)
    return 0


def cmd_consensus(args: argparse.Namespace) -> int:
    provider = SECProvider(SECClient(cache_dir=args.cache_dir, contact=args.contact))
    tickers = [t.upper() for t in args.tickers]
    peers = _resolve_peers(args, tickers)
    snapshots = _build_snapshots(list(dict.fromkeys(tickers + peers)), args, provider=provider)
    results = consensus_grade(snapshots)
    selected = {t: results[t] for t in tickers if t in results}
    render_consensus(selected, console)
    for result in selected.values():
        if not len(result.scores):
            report = next(iter(result.per_profile.values()), None)
            reason = next(iter(report.warnings), "no metrics could be computed") if report else ""
            console.print(f"\n[bold]{result.ticker}[/bold]: [yellow]not gradeable[/yellow] — {reason}")
            continue
        console.print(f"\n[bold]{result.ticker}[/bold] by profile:")
        for name, score in result.scores.sort_values(ascending=False).items():
            report = result.per_profile[name]
            console.print(f"  {name:18} {report.letter:>3}  {score:5.1f}")
    return 0


def cmd_methods(args: argparse.Namespace) -> int:
    from rich.table import Table
    from rich.box import SIMPLE

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
    from rich.table import Table
    from rich.box import SIMPLE

    table = Table(box=SIMPLE, title=f"{len(METRICS)} registered metrics", title_justify="left",
                  header_style="bold")
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
    parser.add_argument("-v", "--verbose", action="store_true", help="show warnings from data layers")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, needs_universe: bool = False) -> None:
        p.add_argument("--profile", default="all_weather", choices=profile_names())
        p.add_argument("--weighting", choices=WEIGHTINGS.names(), help="weighting method at both levels")
        p.add_argument("--normalizer", choices=NORMALIZERS.names())
        p.add_argument("--aggregator", choices=AGGREGATORS.names())
        p.add_argument("--rho", type=float, help="CES compensation parameter (1=mean, 0=geometric)")
        p.add_argument("--curve", choices=["absolute", "cross_sectional", "hybrid"])
        p.add_argument("--sector-neutral", action="store_true", help="score within sector")
        p.add_argument("--universe", required=needs_universe, help="file of tickers, one per line")
        p.add_argument("--no-peers", action="store_true",
                       help="skip the default peer universe (grade will carry no cross-sectional "
                            "information and says so)")
        p.add_argument("--asof", help="grade as of this date (YYYY-MM-DD)")
        p.add_argument("--pit", action="store_true",
                       help="point-in-time: use only figures filed on or before --asof")
        p.add_argument("--price", action="append",
                       help="manual price, TICKER=123.45 (repeatable); enables valuation metrics "
                            "with no market-data feed")
        p.add_argument("--price-dir", help="directory of TICKER.csv price files")
        p.add_argument("--synthetic-prices", action="store_true",
                       help="fabricate a price series where none is available; clearly labelled "
                            "in the report and never real market history")
        p.add_argument("--sec-prices", action=argparse.BooleanOptionalAction, default=True,
                       help="derive prices from SEC insider-transaction filings (default: on) — "
                            "the only keyless price source, sparse but real")
        p.add_argument("--max-price-age", type=int, default=400,
                       help="refuse any SEC-derived price older than this many days (default 400)")
        p.add_argument("--stockanalysis", action="store_true",
                       help="fetch daily adjusted OHLCV from stockanalysis.com, enabling the 40 "
                            "risk/momentum/liquidity metrics. An undocumented endpoint of a "
                            "commercial site, not a licensed feed — read their ToS first")
        p.add_argument("--benchmark", default="SP500",
                       help="FRED index for beta/alpha (SP500, NASDAQ, DJIA); price-only, so alpha "
                            "is overstated by roughly beta x the index dividend yield")
        p.add_argument("--no-network", action="store_true", help="SEC cache only, no price fetches")
        p.add_argument("--refresh", action="store_true", help="bypass the cache")
        p.add_argument("--cache-dir")
        p.add_argument("--contact", help="contact address sent to SEC in the User-Agent")
        p.add_argument("--format", default="text", choices=["text", "json", "md"])

    p_grade = sub.add_parser("grade", help="grade one or more securities")
    p_grade.add_argument("tickers", nargs="+")
    p_grade.add_argument("--explain", action="store_true", help="show per-metric drivers")
    common(p_grade)
    p_grade.set_defaults(func=cmd_grade)

    p_rank = sub.add_parser("rank", help="rank a universe")
    p_rank.add_argument("--top", type=int)
    common(p_rank, needs_universe=True)
    p_rank.set_defaults(func=cmd_rank)

    p_cons = sub.add_parser("consensus", help="grade under every profile and report disagreement")
    p_cons.add_argument("tickers", nargs="+")
    common(p_cons)
    p_cons.set_defaults(func=cmd_consensus)

    p_methods = sub.add_parser("methods", help="list weighting methods, normalizers, aggregators")
    p_methods.set_defaults(func=cmd_methods)

    p_metrics = sub.add_parser("metrics", help="list registered metrics")
    p_metrics.add_argument("--pillar")
    p_metrics.set_defaults(func=cmd_metrics)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.ERROR,
        format="[%(levelname)s] %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        return 130
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]error:[/red] {exc}")
        if getattr(args, "verbose", False):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
