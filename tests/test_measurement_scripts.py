from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from scripts.measure_peer_widening import load_snapshots, measure_peer_widening
from scripts.measure_wide_coverage import compare_coverage_panels

from stock_grader.types import Fundamentals, SectorClass, SecuritySnapshot


def _peer_snapshots() -> list[SecuritySnapshot]:
    empty = pd.DataFrame()
    return [
        SecuritySnapshot(
            ticker=f"T{index}",
            asof=date(2026, 7, 30),
            fundamentals=Fundamentals(
                empty.copy(),
                empty.copy(),
                pd.Series(dtype="object"),
            ),
            sic=f"35{index:02d}",
            sector=SectorClass.GENERAL,
            price=float(100 + 10 * index),
            shares_outstanding=1.0,
            cik=str(index + 1),
        )
        for index in range(8)
    ]


def test_peer_widening_measurement_is_deterministic_and_uses_real_snapshots() -> None:
    first = measure_peer_widening(
        _peer_snapshots(),
        sample_size=4,
        seed=7,
        minimum=2,
        maximum=4,
        size_band_multiple=5.0,
    )
    second = measure_peer_widening(
        _peer_snapshots(),
        sample_size=4,
        seed=7,
        minimum=2,
        maximum=4,
        size_band_multiple=5.0,
    )

    assert first == second
    assert first["measured_sample_size"] == 4
    assert sum(first["fill_pass_distribution"].values()) == 4
    assert all(row["peer_count"] >= 2 for row in first["targets"])
    assert first["disclaimer"].endswith("not investment advice.")


@pytest.mark.parametrize("market_cap", ["not-a-number", None])
def test_peer_snapshot_table_rejects_unparseable_market_cap(tmp_path, market_cap) -> None:
    path = tmp_path / "snapshots.csv"
    pd.DataFrame(
        {
            "ticker": ["A"],
            "asof": ["2026-07-30"],
            "sic": [3571],
            "sector": ["general"],
            "market_cap": [market_cap],
            "fundamentals_available": [True],
        }
    ).to_csv(path, index=False)

    with pytest.raises(ValueError, match="market_cap"):
        load_snapshots(path)


def _coverage_panel(coverage: list[float], graded: list[bool]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "signal_date": ["2026-07-30"] * len(coverage),
            "profile": ["all_weather"] * len(coverage),
            "coverage": coverage,
            "graded": graded,
        }
    )


def test_wide_coverage_measurement_reports_observed_change_and_histogram() -> None:
    narrow = _coverage_panel([0.4, 0.6], [True, False])
    wide = _coverage_panel([0.2, 0.4, 0.6, 0.8], [False, True, True, True])

    result = compare_coverage_panels(narrow, wide)

    assert result["narrow"]["graded_fraction"] == pytest.approx(0.5)
    assert result["wide"]["graded_fraction"] == pytest.approx(0.75)
    assert result["change"]["graded_fraction"] == pytest.approx(0.25)
    assert sum(bucket["count"] for bucket in result["wide"]["coverage_histogram"]) == 4
    assert result["warnings"] == []


def test_wide_coverage_measurement_refuses_bad_numeric_and_metadata_mismatch() -> None:
    narrow = _coverage_panel([0.4, 0.6], [True, False])
    wide = _coverage_panel([0.5, 0.7], [True, True])
    wide["signal_date"] = "2026-07-31"

    with pytest.raises(ValueError, match="signal_date differs"):
        compare_coverage_panels(narrow, wide)
    allowed = compare_coverage_panels(narrow, wide, allow_mismatch=True)
    assert allowed["warnings"]

    narrow["coverage"] = pd.Series(["bad", "0.6"], dtype="object")
    with pytest.raises(ValueError, match="unparseable numeric"):
        compare_coverage_panels(narrow, wide, allow_mismatch=True)
