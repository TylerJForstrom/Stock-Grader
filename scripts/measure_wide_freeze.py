"""Run one wide freeze stage with low-overhead wall-clock and memory instrumentation."""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

import pandas as pd

from stock_grader import cli, pipeline


def _peak_rss_bytes() -> int:
    """Return this process's peak resident set on Windows."""

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCountersEx),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    success = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    )
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.PeakWorkingSetSize)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--foundry", required=True)
    parser.add_argument("--contact", required=True)
    parser.add_argument("--timing-output", required=True)
    args = parser.parse_args(argv)
    root = Path(args.out)
    preexisting_panels = {path.resolve() for path in root.glob("*/*.parquet")}

    measurements: dict[str, object] = {
        "snapshot_seconds": 0.0,
        "matrix_seconds": 0.0,
        "profile_residual_seconds": [],
        "snapshot_count": 0,
        "unresolved_cik_count": 0,
    }
    original_snapshots = cli._build_snapshots
    original_matrix = pipeline.build_metric_matrix
    original_residual = pipeline._grade_from_matrix

    def timed_snapshots(*call_args, **call_kwargs):
        started = time.perf_counter()
        result = original_snapshots(*call_args, **call_kwargs)
        measurements["snapshot_seconds"] = time.perf_counter() - started
        measurements["snapshot_count"] = len(result)
        measurements["unresolved_cik_count"] = sum(
            not getattr(item, "cik", None) for item in result
        )
        return result

    def timed_matrix(*call_args, **call_kwargs):
        started = time.perf_counter()
        result = original_matrix(*call_args, **call_kwargs)
        measurements["matrix_seconds"] = time.perf_counter() - started
        return result

    def timed_residual(*call_args, **call_kwargs):
        started = time.perf_counter()
        result = original_residual(*call_args, **call_kwargs)
        residuals = measurements["profile_residual_seconds"]
        assert isinstance(residuals, list)
        residuals.append(time.perf_counter() - started)
        return result

    cli._build_snapshots = timed_snapshots
    pipeline.build_metric_matrix = timed_matrix
    pipeline._grade_from_matrix = timed_residual

    started = time.perf_counter()
    try:
        exit_code = cli.main(
            [
                "freeze",
                "--all-profiles",
                "--universe",
                args.universe,
                "--out",
                args.out,
                "--bulk-facts",
                "auto",
                "--price-provider",
                "sec",
                "--foundry",
                args.foundry,
                "--contact",
                args.contact,
            ]
        )
    finally:
        cli._build_snapshots = original_snapshots
        pipeline.build_metric_matrix = original_matrix
        pipeline._grade_from_matrix = original_residual
    measurements["total_seconds"] = time.perf_counter() - started
    residuals = measurements["profile_residual_seconds"]
    assert isinstance(residuals, list)
    measurements["profile_residual_total_seconds"] = sum(residuals)
    measurements["profile_residual_mean_seconds"] = (
        sum(residuals) / len(residuals) if residuals else 0.0
    )
    measurements["peak_rss_bytes"] = _peak_rss_bytes()
    panels = sorted(
        path
        for path in root.glob("*/*.parquet")
        if path.resolve() not in preexisting_panels
    )
    measurements["panel_files"] = len(panels)
    measurements["panel_bytes"] = sum(path.stat().st_size for path in panels)
    rows = [pd.read_parquet(path, columns=["graded"]) for path in panels]
    row_count = sum(len(frame) for frame in rows)
    graded_count = sum(int(frame["graded"].sum()) for frame in rows)
    measurements["panel_rows"] = row_count
    measurements["graded_rows"] = graded_count
    measurements["graded_fraction"] = graded_count / row_count if row_count else 0.0
    measurements["exit_code"] = exit_code

    encoded = json.dumps(measurements, indent=2, sort_keys=True, allow_nan=False) + "\n"
    Path(args.timing_output).write_text(encoded, encoding="utf-8")
    print("M5_TIMING=" + json.dumps(measurements, sort_keys=True, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
