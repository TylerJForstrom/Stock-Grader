#!/usr/bin/env python3
"""Stress the model-sensitivity band against artificial missing-data perturbations.

The filename is retained for compatibility, but this is **not statistical calibration**. There is
no observed true stock grade here. The script computes a full-data model score in the synthetic
test universe, hides a random share of fundamental columns, reruns the model, and reports how often
the degraded run's sensitivity band contains that full-data model output.

    python scripts/calibrate_intervals.py --trials 12

This is useful for catching implementation regressions and brittle missing-data behavior. The
containment rate is not a frequentist coverage estimate, a Bayesian credible probability, a
future-return forecast, or validation on a representative market population.
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
    parser.add_argument(
        "--minimum-containment",
        type=float,
        help=(
            "optional internal regression threshold in [0,1]; there is deliberately no default "
            "or claim that the sensitivity band has statistical coverage"
        ),
    )
    args = parser.parse_args(argv)
    if args.minimum_containment is not None and not 0 <= args.minimum_containment <= 1:
        parser.error("--minimum-containment must be between 0 and 1")

    sys.path.insert(0, "tests")
    from test_pipeline import _universe

    snapshots = _universe(args.universe_size)
    baseline = {t: r.score for t, r in grade_universe(snapshots, GradeConfig(seed=0)).items()}

    print(
        f"missing-data sensitivity stress: {args.trials} trials, "
        f"{args.universe_size} synthetic securities"
    )
    print("This is model self-consistency, not statistical or predictive coverage.\n")
    print(f"  {'masked':>7} {'containment':>11} {'median width':>13} {'n':>6}   status")
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
                if report.sensitivity_interval is None or not np.isfinite(
                    report.sensitivity_interval[0]
                ):
                    continue
                if ticker not in baseline or not np.isfinite(baseline[ticker]):
                    continue
                low, high = report.sensitivity_interval
                widths.append(high - low)
                total += 1
                if low - 1e-9 <= baseline[ticker] <= high + 1e-9:
                    hits += 1
        containment = hits / total if total else float("nan")
        width = float(np.median(widths)) if widths else float("nan")
        ok = total > 0 and np.isfinite(width)
        if args.minimum_containment is not None:
            ok = ok and containment >= args.minimum_containment
        if not ok:
            failures += 1
        print(
            f"  {fraction:7.0%} {containment:11.3f} {width:13.2f} {total:6}   "
            f"{'ok' if ok else 'FAILED STRESS CHECK'}"
        )

    print(
        "\nContainment compares one model output with another under artificial masking. "
        "Do not report it as confidence, probability of correctness, or future performance."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
