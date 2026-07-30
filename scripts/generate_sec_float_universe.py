#!/usr/bin/env python3
"""Emit the pre-registered public SEC-float universe stages from fixture or cached bulk data."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from stock_grader.data.foundry import FoundryDataSource
from stock_grader.data.sec import SECClient
from stock_grader.data.sec_bulk import SECBulkFacts
from stock_grader.data.sec_float_universe import emit_sec_float_universes


def _size(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("stage size must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build public-domain universe membership from SEC EntityPublicFloat",
    )
    parser.add_argument("--stock-data", required=True, help="local Stock-Data clone root")
    parser.add_argument("--spec", default="config/universe_spec.json")
    parser.add_argument("--asof", required=True, help="selection date (YYYY-MM-DD)")
    parser.add_argument("--out-dir", default="config")
    parser.add_argument("--sizes", nargs="+", type=_size, default=[250, 500, 1000])
    parser.add_argument("--bulk-cache", help="directory containing companyfacts_*.zip")
    parser.add_argument("--contact", help="contact address sent to SEC in the User-Agent")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_network and args.refresh:
        raise SystemExit("--refresh cannot be combined with --no-network")
    try:
        asof = date.fromisoformat(args.asof)
    except ValueError as exc:
        raise SystemExit("--asof must be YYYY-MM-DD") from exc
    bulk_cache = Path(args.bulk_cache).resolve() if args.bulk_cache else None
    client = SECClient(
        cache_dir=(bulk_cache.parent if bulk_cache is not None else None),
        contact=args.contact,
        offline=args.no_network,
    )
    bulk = SECBulkFacts(client, cache_dir=bulk_cache)
    if bulk.ensure(refresh=args.refresh) is None:
        raise SystemExit("SEC bulk Companyfacts archive is unavailable")
    outputs = emit_sec_float_universes(
        FoundryDataSource(root=args.stock_data),
        bulk,
        args.spec,
        asof,
        args.out_dir,
        sizes=args.sizes,
    )
    for size, path in outputs.items():
        print(f"N={size}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
