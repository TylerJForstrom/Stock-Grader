#!/usr/bin/env python3
"""Compare measured grading coverage in actual narrow and wide frozen panels.

Both inputs must be CSV or Parquet panels carrying coverage and graded. By default the script also
requires matching signal_date and profile values, because comparing different clocks or scoring
profiles would confound the universe-width effect. No coverage or grade is estimated or imputed.

This is research infrastructure, not investment advice or a recommendation about any security.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = {"coverage", "graded"}
HISTOGRAM_EDGES = np.linspace(0.0, 1.0, 11)


def _read_panel(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise ValueError(f"panel does not exist: {path}")
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


def _validated_panel(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"{label} panel is missing columns: " + ", ".join(missing))
    if frame.empty:
        raise ValueError(f"{label} panel is empty")
    clean = frame.copy()
    try:
        clean["coverage"] = pd.to_numeric(clean["coverage"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} coverage contains an unparseable numeric value") from exc
    coverage = clean["coverage"].astype("float64")
    if (~np.isfinite(coverage) | ~coverage.between(0.0, 1.0)).any():
        raise ValueError(f"{label} coverage must contain only finite values in [0, 1]")
    clean["coverage"] = coverage
    clean["graded"] = _strict_booleans(clean["graded"], column=f"{label}.graded")
    return clean


def _one_value(frame: pd.DataFrame, column: str) -> str | None:
    if column not in frame:
        return None
    values = frame[column].dropna().astype(str).drop_duplicates()
    if len(values) != 1:
        return None
    return str(values.iloc[0])


def _summary(frame: pd.DataFrame) -> dict[str, Any]:
    coverage = frame["coverage"].to_numpy(dtype="float64")
    counts, edges = np.histogram(coverage, bins=HISTOGRAM_EDGES)
    histogram = [
        {
            "lower": float(edges[index]),
            "upper": float(edges[index + 1]),
            "count": int(count),
        }
        for index, count in enumerate(counts)
    ]
    graded_count = int(frame["graded"].sum())
    return {
        "rows": len(frame),
        "graded_count": graded_count,
        "graded_fraction": graded_count / len(frame),
        "coverage_min": float(np.min(coverage)),
        "coverage_median": float(np.median(coverage)),
        "coverage_mean": float(np.mean(coverage)),
        "coverage_max": float(np.max(coverage)),
        "coverage_histogram": histogram,
        "signal_date": _one_value(frame, "signal_date"),
        "profile": _one_value(frame, "profile"),
        "universe_id": _one_value(frame, "universe_id"),
    }


def compare_coverage_panels(
    narrow: pd.DataFrame,
    wide: pd.DataFrame,
    *,
    allow_mismatch: bool = False,
) -> dict[str, Any]:
    """Compare real panel rows, refusing clock/profile confounding by default."""
    narrow_clean = _validated_panel(narrow, label="narrow")
    wide_clean = _validated_panel(wide, label="wide")
    warnings: list[str] = []
    for column in ("signal_date", "profile"):
        narrow_value = _one_value(narrow_clean, column)
        wide_value = _one_value(wide_clean, column)
        if narrow_value is None or wide_value is None:
            message = f"{column} must contain exactly one non-null value in each panel"
            if not allow_mismatch:
                raise ValueError(message)
            warnings.append(message)
        elif narrow_value != wide_value:
            message = f"{column} differs: narrow={narrow_value!r}, wide={wide_value!r}"
            if not allow_mismatch:
                raise ValueError(message)
            warnings.append(message)

    narrow_summary = _summary(narrow_clean)
    wide_summary = _summary(wide_clean)
    return {
        "schema_version": "1.0",
        "narrow": narrow_summary,
        "wide": wide_summary,
        "change": {
            "rows": wide_summary["rows"] - narrow_summary["rows"],
            "graded_fraction": (
                wide_summary["graded_fraction"] - narrow_summary["graded_fraction"]
            ),
            "coverage_median": (
                wide_summary["coverage_median"] - narrow_summary["coverage_median"]
            ),
            "coverage_mean": wide_summary["coverage_mean"] - narrow_summary["coverage_mean"],
        },
        "warnings": warnings,
        "disclaimer": "Measured frozen-panel coverage diagnostics; not investment advice.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("narrow_panel", help="CSV or Parquet panel for the narrow universe")
    parser.add_argument("wide_panel", help="CSV or Parquet panel for the wide universe")
    parser.add_argument(
        "--allow-mismatch",
        action="store_true",
        help="report rather than refuse mismatched/missing signal_date or profile metadata",
    )
    parser.add_argument("--output", help="optional JSON output path; stdout is always emitted")
    args = parser.parse_args(argv)

    narrow_path = Path(args.narrow_panel)
    wide_path = Path(args.wide_panel)
    result = compare_coverage_panels(
        _read_panel(narrow_path),
        _read_panel(wide_path),
        allow_mismatch=args.allow_mismatch,
    )
    result["narrow_path"] = str(narrow_path.resolve())
    result["wide_path"] = str(wide_path.resolve())
    payload = json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
