from __future__ import annotations

import hashlib
import json
from datetime import date

import pandas as pd
import pytest
from scripts.measure_peer_widening import (
    load_snapshots,
    measure_peer_widening,
    measure_sector_key_concentration,
)
from scripts.measure_peer_widening import (
    main as peer_widening_main,
)
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
    assert first["peer_count_distribution"] == {"4": 4}
    assert first["peer_count_quantiles"] == {
        "min": 4.0,
        "p25": 4.0,
        "median": 4.0,
        "p75": 4.0,
        "max": 4.0,
    }
    assert first["insufficient_target_count"] == 0
    assert first["selected_peer_market_cap_counts"] == {
        "with_market_cap": 16,
        "without_market_cap": 0,
        "outside_requested_size_band": 0,
    }
    assert all(row["peer_count"] >= 2 for row in first["targets"])
    assert "sector_key_concentration" in first
    assert first["disclaimer"].endswith("not investment advice.")


def test_missing_cap_peer_survives_relaxed_pass_but_is_never_a_target(tmp_path) -> None:
    path = tmp_path / "snapshots.csv"
    pd.DataFrame(
        {
            "ticker": ["SMALL", "LARGE", "NO_CAP"],
            "asof": ["2026-07-30"] * 3,
            "sic": [3571] * 3,
            "sector": ["general"] * 3,
            "market_cap": [100.0, 1_000.0, None],
            "fundamentals_available": [True] * 3,
        }
    ).to_csv(path, index=False)

    snapshots = load_snapshots(path)
    assert snapshots[2].market_cap is None

    result = measure_peer_widening(
        snapshots,
        sample_size=10,
        seed=7,
        minimum=2,
        maximum=2,
        size_band_multiple=5.0,
    )

    assert result["eligible_target_count"] == 2
    assert {row["target"] for row in result["targets"]} == {"SMALL", "LARGE"}
    assert result["peer_count_distribution"] == {"2": 2}
    assert result["insufficient_target_count"] == 0
    assert result["selected_peer_market_cap_counts"] == {
        "with_market_cap": 2,
        "without_market_cap": 2,
        "outside_requested_size_band": 2,
    }
    assert all(
        row["fill_pass"] == "relaxed size band within 4-digit SIC" for row in result["targets"]
    )
    assert all(row["peers_without_market_cap"] == 1 for row in result["targets"])
    assert all(row["peers_outside_requested_size_band"] == 1 for row in result["targets"])


def test_peer_snapshot_csv_preserves_leading_zero_sic(tmp_path) -> None:
    path = tmp_path / "snapshots.csv"
    pd.DataFrame(
        {
            "ticker": ["AGRI"],
            "asof": ["2026-07-30"],
            "sic": ["0100"],
            "sector": ["general"],
            "market_cap": [100.0],
            "fundamentals_available": [True],
        }
    ).to_csv(path, index=False)

    [snapshot] = load_snapshots(path)

    assert snapshot.sic == "0100"
    stats = measure_sector_key_concentration([snapshot])
    assert stats["keys"]["sic2"]["largest_group_label"] == "01"
    assert stats["keys"]["sic3"]["largest_group_label"] == "010"


def test_peer_widening_cli_output_is_path_and_newline_stable(tmp_path) -> None:
    frame = pd.DataFrame(
        {
            "ticker": ["ALPHA", "BETA", "GAMMA"],
            "asof": ["2026-07-30"] * 3,
            "sic": [3571] * 3,
            "sector": ["general"] * 3,
            "market_cap": [100.0, 200.0, 300.0],
            "fundamentals_available": [True] * 3,
        }
    )
    payloads: list[bytes] = []
    source_bytes = b""
    for directory in (tmp_path / "first", tmp_path / "second"):
        directory.mkdir()
        source = directory / "snapshots.csv"
        output = directory / "measurement.json"
        frame.to_csv(source, index=False)
        source_bytes = source.read_bytes()
        assert (
            peer_widening_main(
                [
                    str(source),
                    "--sample-size",
                    "2",
                    "--minimum",
                    "2",
                    "--maximum",
                    "2",
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
        payloads.append(output.read_bytes())

    assert payloads[0] == payloads[1]
    assert payloads[0].endswith(b"\n")
    assert b"\r\n" not in payloads[0]
    payload = json.loads(payloads[0])
    assert payload["snapshot_table"] == "snapshots.csv"
    assert payload["snapshot_table_sha256"] == hashlib.sha256(source_bytes).hexdigest()


@pytest.mark.parametrize(
    "market_cap",
    ["not-a-number", "nan", "inf", "-inf", 0, -1],
)
def test_peer_snapshot_table_rejects_bad_present_market_cap(
    tmp_path,
    market_cap,
) -> None:
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


def test_sector_key_concentration_is_deterministic_and_correct() -> None:
    snapshots = [
        SecuritySnapshot(
            ticker=ticker,
            asof=date(2026, 7, 30),
            sic=sic,
            sector=sector,
        )
        for ticker, sic, sector in [
            ("A", "3571", SectorClass.GENERAL),
            ("B", "3572", SectorClass.GENERAL),
            ("C", "3580", SectorClass.GENERAL),
            ("D", "6021", SectorClass.BANK),
            ("E", None, SectorClass.UTILITY),
            ("F", "7", SectorClass.ENERGY),
        ]
    ]

    first = measure_sector_key_concentration(snapshots)
    second = measure_sector_key_concentration(list(reversed(snapshots)))

    assert first == second
    assert first["snapshot_count"] == 6
    assert first["general_count"] == 3
    assert first["general_fraction"] == pytest.approx(0.5)

    business_model = first["keys"]["business_model"]
    assert business_model["assigned_count"] == 6
    assert business_model["unassigned_count"] == 0
    assert business_model["group_count"] == 4
    assert business_model["largest_group_label"] == "general"
    assert business_model["largest_group_count"] == 3
    assert business_model["largest_group_fraction"] == pytest.approx(0.5)
    assert business_model["hhi"] == pytest.approx(1.0 / 3.0)
    assert business_model["singleton_group_count"] == 3
    assert business_model["singleton_name_count"] == 3
    assert business_model["groups_below_5"] == 4
    assert business_model["names_below_5"] == 6
    assert business_model["groups_below_15"] == 4
    assert business_model["names_below_15"] == 6

    sic2 = first["keys"]["sic2"]
    assert sic2["assigned_count"] == 4
    assert sic2["unassigned_count"] == 2
    assert sic2["group_count"] == 2
    assert sic2["largest_group_label"] == "35"
    assert sic2["largest_group_count"] == 3
    assert sic2["largest_group_fraction"] == pytest.approx(0.75)
    assert sic2["hhi"] == pytest.approx(0.625)
    assert sic2["group_size_quantiles"] == {
        "min": 1.0,
        "p25": 1.5,
        "median": 2.0,
        "p75": 2.5,
        "max": 3.0,
    }
    assert sic2["singleton_group_count"] == 1
    assert sic2["singleton_name_count"] == 1
    assert sic2["groups_below_5"] == 2
    assert sic2["names_below_5"] == 4
    assert sic2["groups_below_15"] == 2
    assert sic2["names_below_15"] == 4
    assert sic2["shrink_weight_n_over_n_plus_5_quantiles"]["min"] == pytest.approx(1.0 / 6.0)
    assert sic2["shrink_weight_n_over_n_plus_5_quantiles"]["max"] == pytest.approx(3.0 / 8.0)

    sic3 = first["keys"]["sic3"]
    assert sic3["assigned_count"] == 4
    assert sic3["unassigned_count"] == 2
    assert sic3["group_count"] == 3
    assert sic3["largest_group_label"] == "357"
    assert sic3["largest_group_count"] == 2
    assert sic3["largest_group_fraction"] == pytest.approx(0.5)
    assert sic3["hhi"] == pytest.approx(0.375)
    assert sic3["singleton_group_count"] == 2
    assert sic3["singleton_name_count"] == 2


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
