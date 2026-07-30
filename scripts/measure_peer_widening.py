#!/usr/bin/env python3
"""Measure how automatic peer selection widens in a real, loaded universe.

The input is an exported CSV or Parquet table from the same SecuritySnapshot set used for a wide
run. Required columns are ticker, asof, sic, sector, market_cap and fundamentals_available; cik and
currency are optional. The script never substitutes synthetic companies, prices, or classifications.
It raises on malformed fields and emits measured JSON suitable for transcription into
docs/UNIVERSE.md.

This is research infrastructure, not investment advice or a recommendation about any security.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stock_grader.peers import select_peers
from stock_grader.types import Fundamentals, SectorClass, SecuritySnapshot

REQUIRED_COLUMNS = {
    "ticker",
    "asof",
    "sic",
    "sector",
    "market_cap",
    "fundamentals_available",
}


def _read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise ValueError(f"snapshot table does not exist: {path}")
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _strict_booleans(values: pd.Series, *, column: str) -> pd.Series:
    truthy = {"1", "true", "yes", "y"}
    falsey = {"0", "false", "no", "n"}
    parsed: list[bool] = []
    for value in values:
        token = str(value).strip().lower()
        if token in truthy:
            parsed.append(True)
        elif token in falsey:
            parsed.append(False)
        else:
            raise ValueError(f"{column} contains an unparseable boolean: {value!r}")
    return pd.Series(parsed, index=values.index, dtype="bool")


def load_snapshots(path: str | Path) -> list[SecuritySnapshot]:
    """Reconstruct selection-relevant snapshots from an auditable metadata export."""
    source = Path(path)
    frame = _read_table(source)
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError("snapshot table is missing columns: " + ", ".join(missing))
    if frame.empty:
        raise ValueError("snapshot table is empty")

    tickers = frame["ticker"].astype("string").str.strip().str.upper()
    if tickers.isna().any() or tickers.eq("").any():
        raise ValueError("ticker contains blank or missing values")
    if tickers.duplicated().any():
        repeated = sorted(tickers[tickers.duplicated(keep=False)].unique())
        raise ValueError("snapshot table contains duplicate tickers: " + ", ".join(repeated))

    asof = pd.to_datetime(frame["asof"], errors="coerce")
    if asof.isna().any():
        raise ValueError("asof contains invalid dates")
    if asof.dt.normalize().nunique() != 1:
        raise ValueError("all snapshot rows must share one asof date")

    try:
        market_caps = pd.to_numeric(frame["market_cap"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("market_cap contains an unparseable numeric value") from exc
    invalid_cap = market_caps.isna() | (
        ~np.isfinite(market_caps.astype("float64")) | (market_caps <= 0.0)
    )
    if invalid_cap.any():
        raise ValueError("market_cap must contain only finite positive values")

    fundamentals_available = _strict_booleans(
        frame["fundamentals_available"],
        column="fundamentals_available",
    )
    empty = pd.DataFrame()
    snapshots: list[SecuritySnapshot] = []
    for index in frame.index:
        try:
            sector = SectorClass(str(frame.at[index, "sector"]).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown sector for {tickers.at[index]}: {frame.at[index, 'sector']!r}") from exc
        cap = market_caps.at[index]
        has_cap = pd.notna(cap)
        fundamentals = (
            Fundamentals(empty.copy(), empty.copy(), pd.Series(dtype="object"))
            if fundamentals_available.at[index]
            else None
        )
        snapshots.append(
            SecuritySnapshot(
                ticker=str(tickers.at[index]),
                asof=asof.at[index].date(),
                fundamentals=fundamentals,
                cik=(
                    str(frame.at[index, "cik"]).strip()
                    if "cik" in frame and pd.notna(frame.at[index, "cik"])
                    else None
                ),
                sic=(str(frame.at[index, "sic"]).strip() if pd.notna(frame.at[index, "sic"]) else None),
                sector=sector,
                currency=(
                    str(frame.at[index, "currency"]).strip()
                    if "currency" in frame and pd.notna(frame.at[index, "currency"])
                    else "USD"
                ),
                # SecuritySnapshot.market_cap is a derived property. A unit share count preserves
                # the exact exported market cap without pretending this is a quoted share price.
                price=float(cap) if has_cap else None,
                shares_outstanding=1.0 if has_cap else None,
            )
        )
    return snapshots


def _quantiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    array = np.asarray(values, dtype="float64")
    return {
        "min": float(np.min(array)),
        "p25": float(np.quantile(array, 0.25)),
        "median": float(np.median(array)),
        "p75": float(np.quantile(array, 0.75)),
        "max": float(np.max(array)),
    }


def measure_peer_widening(
    snapshots: list[SecuritySnapshot],
    *,
    sample_size: int = 50,
    seed: int = 0,
    minimum: int = 8,
    maximum: int = 30,
    size_band_multiple: float = 5.0,
) -> dict[str, Any]:
    """Run the production peer selector and summarize its observed widening behavior."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    eligible = sorted(
        (
            snapshot
            for snapshot in snapshots
            if snapshot.fundamentals is not None and snapshot.market_cap is not None
        ),
        key=lambda snapshot: snapshot.ticker,
    )
    if not eligible:
        raise ValueError("no snapshots have both fundamentals and a finite positive market cap")

    rng = np.random.default_rng(seed)
    count = min(sample_size, len(eligible))
    selected_indexes = sorted(int(index) for index in rng.choice(len(eligible), size=count, replace=False))
    targets = [eligible[index] for index in selected_indexes]

    rows: list[dict[str, Any]] = []
    fill_passes: Counter[str] = Counter()
    target_relative_minima: list[float] = []
    target_relative_maxima: list[float] = []
    within_set_spreads: list[float] = []
    for target in targets:
        peers, manifest = select_peers(
            target,
            [candidate for candidate in snapshots if candidate is not target],
            minimum=minimum,
            maximum=maximum,
            size_band_multiple=size_band_multiple,
            candidate_universe="wide_snapshot_table",
        )
        fill_pass = (
            manifest.widening_steps[-1].split(": +", 1)[0]
            if manifest.widening_steps
            else "no eligible pass"
        )
        if len(peers) < minimum:
            fill_pass = "insufficient peers after all passes"
        fill_passes[fill_pass] += 1

        ratios = [
            float(peer.market_cap / target.market_cap)
            for peer in peers
            if peer.market_cap is not None and target.market_cap is not None
        ]
        ratio_min = min(ratios) if ratios else None
        ratio_max = max(ratios) if ratios else None
        spread = ratio_max / ratio_min if ratio_min and ratio_max else None
        if ratio_min is not None:
            target_relative_minima.append(ratio_min)
        if ratio_max is not None:
            target_relative_maxima.append(ratio_max)
        if spread is not None and math.isfinite(spread):
            within_set_spreads.append(spread)
        rows.append(
            {
                "target": target.ticker,
                "peer_count": len(peers),
                "fill_pass": fill_pass,
                "min_peer_to_target_market_cap": ratio_min,
                "max_peer_to_target_market_cap": ratio_max,
                "peer_set_market_cap_spread": spread,
                "selection_fingerprint": manifest.fingerprint,
            }
        )

    return {
        "schema_version": "1.0",
        "universe_size": len(snapshots),
        "eligible_target_count": len(eligible),
        "requested_sample_size": sample_size,
        "measured_sample_size": len(targets),
        "seed": seed,
        "parameters": {
            "minimum": minimum,
            "maximum": maximum,
            "size_band_multiple": size_band_multiple,
        },
        "fill_pass_distribution": dict(sorted(fill_passes.items())),
        "min_peer_to_target_market_cap_quantiles": _quantiles(target_relative_minima),
        "max_peer_to_target_market_cap_quantiles": _quantiles(target_relative_maxima),
        "peer_set_market_cap_spread_quantiles": _quantiles(within_set_spreads),
        "targets": rows,
        "disclaimer": "Measured peer-selection diagnostics; not investment advice.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_table", help="CSV or Parquet export of loaded snapshot metadata")
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--minimum", type=int, default=8)
    parser.add_argument("--maximum", type=int, default=30)
    parser.add_argument("--size-band-multiple", type=float, default=5.0)
    parser.add_argument("--output", help="optional JSON output path; stdout is always emitted")
    args = parser.parse_args(argv)

    result = measure_peer_widening(
        load_snapshots(args.snapshot_table),
        sample_size=args.sample_size,
        seed=args.seed,
        minimum=args.minimum,
        maximum=args.maximum,
        size_band_multiple=args.size_band_multiple,
    )
    result["snapshot_table"] = str(Path(args.snapshot_table).resolve())
    payload = json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
