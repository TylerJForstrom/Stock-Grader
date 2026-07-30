from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from scripts import measure_wide_freeze


def _run_mocked_harness(
    tmp_path: Path,
    monkeypatch,
    capsys,
    *,
    run_name: str,
) -> tuple[int, bytes, str, list[str]]:
    output_root = tmp_path / f"{run_name}-panels"
    timing_output = tmp_path / f"{run_name}-timing.json"
    universe = tmp_path / "universe.txt"
    foundry = tmp_path / "Stock-Data"
    read_profiles: list[str] = []
    stale = output_root / "stale_profile" / "1999-12-31.parquet"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale panel from a prior invocation")

    def fake_snapshots(*_args, **_kwargs):
        return [SimpleNamespace(cik="1"), SimpleNamespace(cik=None)]

    def fake_matrix(*_args, **_kwargs):
        return pd.DataFrame({"metric": [1.0, 2.0]})

    def fake_residual(*_args, **_kwargs):
        return {"A": object(), "B": object()}

    expected_cli_args = [
        "freeze",
        "--all-profiles",
        "--universe",
        str(universe),
        "--out",
        str(output_root),
        "--bulk-facts",
        "auto",
        "--price-provider",
        "sec",
        "--foundry",
        str(foundry),
        "--contact",
        "researcher@example.test",
    ]

    def fake_cli_main(argv: list[str]) -> int:
        assert argv == expected_cli_args
        snapshots = measure_wide_freeze.cli._build_snapshots(
            ["A", "B"],
            SimpleNamespace(),
            provider=object(),
        )
        measure_wide_freeze.pipeline.build_metric_matrix(snapshots)
        measure_wide_freeze.pipeline._grade_from_matrix(snapshots)
        measure_wide_freeze.pipeline._grade_from_matrix(snapshots)
        first = output_root / "a_profile" / "2099-01-01.parquet"
        second = output_root / "z_profile" / "2099-01-01.parquet"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(b"ab")
        second.write_bytes(b"cde")
        return 7

    def fake_read_parquet(path: Path, *, columns: list[str]) -> pd.DataFrame:
        assert columns == ["graded"]
        read_profiles.append(path.parent.name)
        if path.parent.name == "a_profile":
            return pd.DataFrame({"graded": [True, False]})
        return pd.DataFrame({"graded": [True, True, False]})

    clock = iter([0.0, 10.0, 12.0, 20.0, 25.0, 30.0, 31.5, 40.0, 42.5, 50.0])
    with monkeypatch.context() as patch:
        patch.setattr(measure_wide_freeze.cli, "_build_snapshots", fake_snapshots)
        patch.setattr(measure_wide_freeze.pipeline, "build_metric_matrix", fake_matrix)
        patch.setattr(measure_wide_freeze.pipeline, "_grade_from_matrix", fake_residual)
        patch.setattr(measure_wide_freeze.cli, "main", fake_cli_main)
        patch.setattr(measure_wide_freeze.time, "perf_counter", lambda: next(clock))
        patch.setattr(measure_wide_freeze, "_peak_rss_bytes", lambda: 8192)
        patch.setattr(measure_wide_freeze.pd, "read_parquet", fake_read_parquet)

        exit_code = measure_wide_freeze.main(
            [
                "--universe",
                str(universe),
                "--out",
                str(output_root),
                "--foundry",
                str(foundry),
                "--contact",
                "researcher@example.test",
                "--timing-output",
                str(timing_output),
            ]
        )
        assert measure_wide_freeze.cli._build_snapshots is fake_snapshots
        assert measure_wide_freeze.pipeline.build_metric_matrix is fake_matrix
        assert measure_wide_freeze.pipeline._grade_from_matrix is fake_residual

    stdout = capsys.readouterr().out
    return exit_code, timing_output.read_bytes(), stdout, read_profiles


def test_wide_freeze_harness_is_deterministic_and_propagates_exit_code(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    first = _run_mocked_harness(
        tmp_path,
        monkeypatch,
        capsys,
        run_name="first",
    )
    second = _run_mocked_harness(
        tmp_path,
        monkeypatch,
        capsys,
        run_name="second",
    )

    assert first == second
    exit_code, encoded, stdout, read_profiles = first
    assert exit_code == 7
    assert read_profiles == ["a_profile", "z_profile"]

    payload = json.loads(encoded)
    assert set(payload) == {
        "exit_code",
        "graded_fraction",
        "graded_rows",
        "matrix_seconds",
        "panel_bytes",
        "panel_files",
        "panel_rows",
        "peak_rss_bytes",
        "profile_residual_mean_seconds",
        "profile_residual_seconds",
        "profile_residual_total_seconds",
        "snapshot_count",
        "snapshot_seconds",
        "total_seconds",
        "unresolved_cik_count",
    }
    assert payload == {
        "exit_code": 7,
        "graded_fraction": 0.6,
        "graded_rows": 3,
        "matrix_seconds": 5.0,
        "panel_bytes": 5,
        "panel_files": 2,
        "panel_rows": 5,
        "peak_rss_bytes": 8192,
        "profile_residual_mean_seconds": 2.0,
        "profile_residual_seconds": [1.5, 2.5],
        "profile_residual_total_seconds": 4.0,
        "snapshot_count": 2,
        "snapshot_seconds": 2.0,
        "total_seconds": 50.0,
        "unresolved_cik_count": 1,
    }
    expected_file = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    expected_stdout = "M5_TIMING=" + json.dumps(payload, sort_keys=True) + "\n"
    assert encoded == expected_file.encode()
    assert stdout == expected_stdout
