#!/usr/bin/env python3
"""Measure whether the grader detects distress, using free SEC outcome labels.

No prices and no forward returns are needed. EDGAR full-text search finds companies whose auditors
issued a going-concern opinion — an expert stating substantial doubt the company survives twelve
months — and the grader is run against them **point-in-time as of the filing date**, so it sees only
what was public then.

    python scripts/validate_distress.py --n 60 --year 2023

Measured on 2026-07-24 with 60 labelled companies against 29 large-cap controls:

    metric                  AUC
    ohlson_o_score          1.00   perfect separation
    interest_coverage       0.99
    net_margin              0.99
    roa                     0.98
    altman_z_prime          0.95
    piotroski_f_score       0.95
    fcf_to_debt             0.91
    current_ratio           0.83
    accruals_ratio          0.29   <- BROKEN; found and fixed by this script

That last row is the point of the exercise. A large net loss drives ``NI - CFO`` sharply negative,
which a lower-is-better metric read as conservative accounting, so the metric was *rewarding*
distressed companies. It is now gated on positive earnings, as Sloan's original work assumes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

# Importing these populates the metric registry.
from stock_grader import aggregate, normalize, weighting  # noqa: F401
from stock_grader.data.sec import SECClient, SECProvider, build_fundamentals
from stock_grader.data.sectors import classify_sic
from stock_grader.metrics import fundamental, models, statistical  # noqa: F401
from stock_grader.metrics.engine import evaluate_metrics
from stock_grader.registry import METRICS
from stock_grader.types import PitMode, SecuritySnapshot
from stock_grader.validation import EdgarLabelSearch, separation_report

# Large, liquid, unambiguously going concerns — the negative class.
CONTROLS = [
    "AAPL", "MSFT", "JNJ", "PG", "KO", "WMT", "XOM", "CVX", "HD", "MCD",
    "COST", "PEP", "LLY", "ABBV", "MRK", "UNH", "CAT", "HON", "LMT", "UPS",
    "LIN", "NEE", "SO", "DUK", "T", "VZ", "ADBE", "CRM", "TXN", "QCOM",
]

# Metrics that should carry information about solvency.
WATCHED = [
    "ohlson_o_score", "altman_z", "altman_z_prime", "piotroski_f_score",
    "current_ratio", "quick_ratio", "interest_coverage", "debt_to_assets",
    "net_margin", "roa", "roe", "fcf_to_debt", "cash_conversion",
    "accruals_ratio", "fcf_to_net_income", "debt_to_equity",
]


def build_snapshot(client: SECClient, cik: str, name: str, asof: date) -> SecuritySnapshot | None:
    """Point-in-time snapshot: only facts filed on or before ``asof``."""
    facts = client.company_facts(cik)
    if not facts:
        return None
    fundamentals = build_fundamentals(facts, pit_mode=PitMode.PIT, asof=asof)
    if fundamentals.quarterly.empty and fundamentals.annual.empty:
        return None
    submissions = client.submissions(cik) or {}
    snapshot = SecuritySnapshot(
        ticker=name, asof=asof, fundamentals=fundamentals, cik=cik,
        sector=classify_sic(submissions.get("sic")),
    )
    snapshot.shares_outstanding = fundamentals.ttm("shares_diluted")
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=60, help="labelled companies to fetch")
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--label", default="going_concern",
                        choices=["going_concern", "bankruptcy", "restatement", "material_weakness"])
    parser.add_argument("--min-sample", type=int, default=5,
                        help="skip metrics with fewer usable observations")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR,
                        format="[%(levelname)s] %(message)s")

    asof = date(args.year, 12, 31)
    client = SECClient()
    provider = SECProvider(client)

    print(f"searching EDGAR for {args.label} filings in {args.year}...")
    labels = EdgarLabelSearch().search(
        args.label, start=date(args.year, 1, 1), end=date(args.year, 12, 31), limit=args.n
    )
    if not labels:
        print("no labelled companies found (EDGAR full-text search unreachable?)", file=sys.stderr)
        return 1
    print(f"  {len(labels)} companies labelled")

    print(f"grading them point-in-time as of {asof}...")
    distressed: dict[str, dict] = {}
    for item in labels:
        snapshot = build_snapshot(client, item.cik, item.ticker or item.cik, asof)
        if snapshot is not None:
            distressed[item.ticker or item.cik] = evaluate_metrics(snapshot)

    print(f"grading {len(CONTROLS)} controls at the same date...")
    healthy: dict[str, dict] = {}
    for ticker in CONTROLS:
        cik = provider.resolve_cik(ticker)
        if not cik:
            continue
        snapshot = build_snapshot(client, cik, ticker, asof)
        if snapshot is not None:
            healthy[ticker] = evaluate_metrics(snapshot)

    print(f"\nusable: {len(distressed)} distressed, {len(healthy)} control\n")
    header = f"{'metric':24} {'n_dist':>7} {'n_ctrl':>7} {'med_dist':>11} {'med_ctrl':>11} {'AUC':>6}"
    print(header)
    print("-" * len(header))

    rows = []
    for name in WATCHED:
        spec = METRICS.maybe(name)
        if spec is None:
            continue
        left = {k: (v[name].value if name in v else None) for k, v in distressed.items()}
        right = {k: (v[name].value if name in v else None) for k, v in healthy.items()}
        report = separation_report(left, right, metric_name=name)
        if report["n_distressed"] < args.min_sample or report["n_healthy"] < args.min_sample:
            print(f"{name:24} {report['n_distressed']:7} {report['n_healthy']:7}"
                  f"   too few usable observations")
            continue
        area = report["auc"]
        # A lower-is-better metric should be HIGHER for distressed companies; flip so that in every
        # row "AUC above 0.5" means the metric points the right way.
        if spec.direction < 0:
            area = 1.0 - area
        rows.append((name, area))
        flag = "  <- INVERTED" if area < 0.45 else ""
        print(f"{name:24} {report['n_distressed']:7} {report['n_healthy']:7} "
              f"{report['median_distressed']:11.3f} {report['median_healthy']:11.3f} {area:6.2f}{flag}")

    print("\nAUC = P(a distressed company looks worse than a healthy one).")
    print("0.5 is no signal. Below ~0.45 the metric is pointing the wrong way and is a real defect —")
    print("it is actively rewarding the companies it should be penalising.")
    inverted = [n for n, a in rows if a < 0.45]
    if inverted:
        print(f"\nINVERTED METRICS: {', '.join(inverted)}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
