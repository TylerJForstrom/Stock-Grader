#!/usr/bin/env python3
"""Does the reported "90% confidence interval" actually cover 90%?

An interval is a testable claim, and this is the test. There is no external truth for a stock
grade, but there is an operational one: the grade computed from **all** available data. Hide a
random k% of metrics, regrade, and ask how often the degraded interval contains the full-data
score. A calibrated 90% interval should manage that about 90% of the time at every level of
degradation.

    python scripts/calibrate_intervals.py --trials 12

Why this exists: the interval used to be produced by mapping the raw one affinely,
``w*low + (1-w)*percentile``, which added the same constant to both ends. That halved the
half-width (``absolute_weight`` is 0.5) and gave the percentile *zero* width even though the
percentile is a function of the same resampled composite. Measured coverage of the advertised 90%
was 0.697 / 0.551 / 0.439 / 0.395 as 10/20/30/40% of pillars were masked — not a 90% interval at
any level, and increasingly wrong the less data there was.

The fix ranks each resampled draw against the fixed peer set before taking percentiles, so the
percentile half carries its real width. This script is how you check that claim rather than assume it.
"""

from __future__ import annotations

import argparse
import copy
import sys

import numpy as np

# Importing these populates the registries.
from stock_grader import aggregate, normalize, weighting  # noqa: F401
from stock_grader.metrics import fundamental, models, statistical  # noqa: F401
from stock_grader.pipeline import GradeConfig, grade_universe


def mask_metrics(snapshots, fraction: float, rng: np.random.Generator):
    """Hide a random fraction of each security's *computable* metrics.

    Masking is applied by blanking the underlying fundamentals columns, so the pipeline genuinely
    cannot compute those metrics rather than being told to skip them.
    """
    out = []
    for snapshot in snapshots:
        clone = copy.copy(snapshot)
        if clone.fundamentals is None or fraction <= 0:
            out.append(clone)
            continue
        fundamentals = copy.copy(clone.fundamentals)
        columns = list(fundamentals.quarterly.columns)
        if columns:
            n_drop = int(round(len(columns) * fraction))
            if n_drop:
                drop = rng.choice(len(columns), size=n_drop, replace=False)
                quarterly = fundamentals.quarterly.copy()
                quarterly.iloc[:, drop] = np.nan
                fundamentals.quarterly = quarterly
        clone.fundamentals = fundamentals
        out.append(clone)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--universe-size", type=int, default=30)
    parser.add_argument("--target", type=float, default=0.90)
    parser.add_argument("--tolerance", type=float, default=0.06)
    args = parser.parse_args(argv)

    sys.path.insert(0, "tests")
    from test_pipeline import _universe

    snapshots = _universe(args.universe_size)
    truth = {t: r.score for t, r in grade_universe(snapshots, GradeConfig(seed=0)).items()}

    print(f"target coverage {args.target:.0%}, {args.trials} trials, "
          f"{args.universe_size} securities\n")
    print(f"  {'masked':>7} {'coverage':>9} {'median width':>13} {'n':>6}   verdict")
    print("  " + "-" * 52)

    failures = 0
    for fraction in (0.0, 0.10, 0.20, 0.30, 0.40):
        hits = 0
        total = 0
        widths = []
        for trial in range(args.trials):
            rng = np.random.default_rng(1000 + trial)
            degraded = mask_metrics(snapshots, fraction, rng)
            reports = grade_universe(degraded, GradeConfig(seed=trial))
            for ticker, report in reports.items():
                if report.ci is None or not np.isfinite(report.ci[0]):
                    continue
                if ticker not in truth or not np.isfinite(truth[ticker]):
                    continue
                low, high = report.ci
                widths.append(high - low)
                total += 1
                if low - 1e-9 <= truth[ticker] <= high + 1e-9:
                    hits += 1
        coverage = hits / total if total else float("nan")
        width = float(np.median(widths)) if widths else float("nan")
        ok = abs(coverage - args.target) <= args.tolerance if total else False
        if not ok:
            failures += 1
        print(f"  {fraction:7.0%} {coverage:9.3f} {width:13.2f} {total:6}   "
              f"{'ok' if ok else 'MISCALIBRATED'}")

    print("\ncoverage below target means the interval is too narrow and the grade is being sold "
          "as\nmore certain than it is; above target means it is too wide to be useful.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
