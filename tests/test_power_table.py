"""End-to-end checks of the planted-IC power-table harness.

A tiny grid is generated with the grader's OWN synthetic price module
(``stock_grader.data.synthetic``), scores are planted against the realized
forward-return ranks at a known blend, and each replication runs through the
production ``backtest`` CLI path exactly as ``scripts/power_table.py`` does for
the real calibration grid.  The assertions are the two facts that make a power
table trustworthy at all: the gate fires on an enormous planted signal, and it
stays quiet at the null roughly at its alpha.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pandas as pd
import pytest

from scripts.power_table import (
    PRODUCTION_BACKTEST_FLAGS,
    REPO_ROOT,
    CellResult,
    assert_outside_repo,
    backtest_flags,
    evaluate_cell_frame,
    evaluate_seed_panel,
    render_band_markdown,
    smallest_detectable_ic,
    summarize_cell,
    write_artifacts,
)
from stock_grader.data.synthetic import generate_prices
from stock_grader.significance import norm_ppf

GATE_ALPHA = 0.05


def _planted_panel(
    *, n_names: int, n_months: int, rho: float, seed: int
) -> pd.DataFrame:
    """A frozen-score panel whose score/forward-return rank correlation is planted.

    Prices come from ``generate_prices`` (the repo's own synthetic generator);
    the score is ``rho * blom(forward-return rank) + sqrt(1-rho^2) * noise``,
    the same construction the Stock-Market-Sim calibration grid documents.
    """

    rng = np.random.default_rng(seed)
    n_days = 21 * (n_months + 3)
    closes = {}
    for index in range(n_names):
        ticker = f"SYN{index:03d}"
        prices = generate_prices(
            ticker, n_days=n_days, seed=seed * 100_003 + index, synthetic=True
        )
        closes[ticker] = prices["close"]
    close = pd.DataFrame(closes)
    month_end = close.groupby([close.index.year, close.index.month]).tail(1)
    month_end = month_end.iloc[-(n_months + 1) :]
    if len(month_end) != n_months + 1:
        raise AssertionError("not enough synthetic months generated")

    rows = []
    for position in range(n_months):
        signal_date = month_end.index[position]
        window_end = month_end.index[position + 1]
        forward = (
            month_end.iloc[position + 1] / month_end.iloc[position] - 1.0
        )
        ranks = forward.rank(method="average")
        blom = np.asarray(
            [norm_ppf((r - 0.375) / (n_names + 0.25)) for r in ranks]
        )
        noise = rng.standard_normal(n_names)
        scores = rho * blom + math.sqrt(1.0 - rho * rho) * noise
        for index, ticker in enumerate(month_end.columns):
            rows.append(
                {
                    "signal_date": signal_date,
                    "return_start": signal_date + pd.Timedelta(days=1),
                    "return_end": window_end,
                    "ticker": ticker,
                    "security_id": f"SID{index:05d}",
                    "score": float(scores[index]),
                    "forward_return": float(forward.iloc[index]),
                    "universe_id": "synthetic-test",
                    "universe_is_pit": True,
                    "return_is_total": True,
                    "delisting_return_included": True,
                }
            )
    return pd.DataFrame(rows)


def _stacked_replications(
    *, n_seeds: int, n_names: int, n_months: int, rho: float
) -> pd.DataFrame:
    frames = []
    for replication in range(n_seeds):
        frame = _planted_panel(
            n_names=n_names, n_months=n_months, rho=rho, seed=replication + 1
        )
        frame.insert(0, "seed", replication)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _cell(planted_ic: float, months: int, universe: int) -> dict:
    return {
        "file": f"test_ic{planted_ic}_m{months}_u{universe}.parquet",
        "sha256": "0" * 64,
        "planted_ic": planted_ic,
        "months": months,
        "universe": universe,
    }


def test_power_is_high_at_huge_planted_ic(tmp_path):
    """rho=0.95 over 12 periods x 60 names must be detected essentially always."""

    frame = _stacked_replications(n_seeds=5, n_names=60, n_months=12, rho=0.95)
    outcomes = evaluate_cell_frame(frame, tmp_path / "huge", "huge")
    result = summarize_cell(_cell(0.95, 12, 60), outcomes)
    assert result.n_seeds == 5
    assert result.gate_pass_rate >= 0.9
    # The evaluator must also recover the planted magnitude, not just the sign.
    assert result.mean_realized_rank_ic > 0.7


def test_false_positive_rate_near_alpha_at_null(tmp_path):
    """rho=0 replications must pass the gate at most ~alpha of the time."""

    frame = _stacked_replications(n_seeds=30, n_names=60, n_months=12, rho=0.0)
    outcomes = evaluate_cell_frame(frame, tmp_path / "null", "null")
    result = summarize_cell(_cell(0.0, 12, 60), outcomes)
    assert result.n_seeds == 30
    # The gate is a conjunction (DSR >= 0.95 AND CI low > 0), so its size is
    # at most alpha; allow binomial noise on 30 replications above that.
    assert result.gate_pass_rate <= GATE_ALPHA + 0.05
    assert abs(result.mean_realized_rank_ic) < 0.05


def test_scratch_inside_repo_is_refused(tmp_path):
    with pytest.raises(ValueError, match="inside the repository"):
        assert_outside_repo(REPO_ROOT / "build" / "anywhere", what="scratch")
    # And a location genuinely outside the checkout is accepted.
    assert_outside_repo(tmp_path, what="scratch")


def test_dated_artifacts_are_immutable(tmp_path):
    manifest = {
        "artifact": "test grid",
        "created_utc": "2026-01-01T00:00:00+00:00",
        "code_commit": "deadbeef",
        "synthetic_only": True,
    }
    frame = _stacked_replications(n_seeds=2, n_names=60, n_months=12, rho=0.0)
    outcomes = evaluate_cell_frame(frame, tmp_path / "im", "im")
    results = [summarize_cell(_cell(0.0, 12, 60), outcomes)]
    out_dir = tmp_path / "artifacts"
    written = write_artifacts(out_dir, "1999-01-01", manifest, results)
    assert len(written) == 3
    with pytest.raises(FileExistsError, match="immutable"):
        write_artifacts(out_dir, "1999-01-01", manifest, results)


# --------------------------------------------------------------------------
# Banded grids: the axes the 2026-08-03 table does not have.
# --------------------------------------------------------------------------


def _band_cell(
    planted_ic: float,
    periods: int,
    universe: int,
    *,
    label: str,
    regime: str = "smallcap-heterogeneous",
    cadence: str = "semimonthly",
) -> dict:
    cell = _cell(planted_ic, periods, universe)
    cell.update(label=label, regime=regime, cadence=cadence)
    return cell


def _band_manifest() -> dict:
    return {
        "artifact": "test band grid",
        "created_utc": "2026-01-01T00:00:00+00:00",
        "code_commit": "deadbeef",
        "synthetic_only": True,
        "grid": {"kind": "adv-band detection power", "spec_sha256": "ab" * 32, "n_cells": 2},
        "generator": {
            "regimes": {
                "smallcap": {
                    "annual_vol": 0.45,
                    "annual_vol_log_sd": 0.45,
                    "innovation_df": 3.0,
                },
                "baseline": {
                    "annual_vol": 0.18,
                    "annual_vol_log_sd": 0.0,
                    "innovation_df": 5.0,
                },
            }
        },
    }


def _fake_result(
    planted_ic: float,
    periods: int,
    universe: int,
    *,
    label: str,
    regime: str,
    gate_pass_rate: float,
    n_seeds: int = 20,
) -> CellResult:
    """A CellResult built directly, so renderer tests need no backtest runs."""

    return CellResult(
        planted_ic=planted_ic,
        months=periods,
        universe=universe,
        file=f"panel_{label}_{regime}_{periods}_{planted_ic}.parquet",
        input_sha256="0" * 64,
        n_seeds=n_seeds,
        gate_pass_rate=gate_pass_rate,
        dsr_pass_rate=gate_pass_rate,
        ci_low_positive_rate=gate_pass_rate,
        mean_dsr=0.5,
        mean_realized_rank_ic=planted_ic,
        insufficient_sample_verdicts=0,
        label=label,
        regime=regime,
        cadence="semimonthly",
    )


class TestHowReturnScaleReachesTheGate:
    """The load-bearing claim behind the regime axis, pinned both ways.

    The banded artifact tells its reader two things: that a rank IC cannot
    move when returns are rescaled, and that the GATE can — because
    ``evaluate_walk_forward`` deducts a cost in return units
    (``net_spread = gross_spread - cost_rate * turnover``) that does not
    scale with them. Both halves are asserted against the production CLI
    path rather than argued, because the artifact's whole treatment of
    dispersion rests on the distinction.
    """

    # Factors stay above 0 and below the point where the panel validator's
    # "forward_return cannot be below -100%" bound would bite: a total loss
    # is a real floor, so the scale sweep has to respect it.
    FACTORS = (0.25, 2.0, 4.0)

    def test_rank_ic_is_exactly_scale_invariant(self, tmp_path):
        panel = _planted_panel(n_names=80, n_months=14, rho=0.6, seed=7)
        base = evaluate_seed_panel(panel, tmp_path / "ic", "base")
        for factor in self.FACTORS:
            scaled = panel.copy()
            scaled["forward_return"] = scaled["forward_return"] * factor
            other = evaluate_seed_panel(scaled, tmp_path / "ic", f"x{factor}")
            assert other["mean_rank_ic"] == pytest.approx(base["mean_rank_ic"])

    def test_the_fixed_cost_makes_the_gate_scale_dependent(self, tmp_path):
        """Bigger returns against an unchanged charge => a better net Sharpe."""

        panel = _planted_panel(n_names=80, n_months=14, rho=0.6, seed=7)
        dsr = {}
        for factor in (0.25, 1.0, 4.0):
            scaled = panel.copy()
            scaled["forward_return"] = scaled["forward_return"] * factor
            payload = evaluate_seed_panel(scaled, tmp_path / "cost", f"x{factor}")
            dsr[factor] = payload["significance"]["deflated_sharpe"]
        assert dsr[0.25] < dsr[1.0] < dsr[4.0], dsr
        # The effect is real, not float noise.
        assert dsr[4.0] - dsr[0.25] > 1e-4

    def test_without_a_cost_the_gate_is_scale_invariant(self, tmp_path):
        """The other half: the dependence comes from the charge, nothing else.

        Charging zero restores the pure mean/sd algebra, which is what makes
        "the level enters only through the cost ratio" a precise statement
        rather than a hand-wave.
        """

        panel = _planted_panel(n_names=80, n_months=14, rho=0.6, seed=7)
        free = (*backtest_flags(12), "--transaction-cost-bps", "0")
        base = evaluate_seed_panel(panel, tmp_path / "free", "base", flags=free)
        for factor in self.FACTORS:
            scaled = panel.copy()
            scaled["forward_return"] = scaled["forward_return"] * factor
            other = evaluate_seed_panel(
                scaled, tmp_path / "free", f"x{factor}", flags=free
            )
            assert other["significance"]["deflated_sharpe"] == pytest.approx(
                base["significance"]["deflated_sharpe"], abs=1e-9
            )
            assert (
                other["significance"]["significant"]
                is base["significance"]["significant"]
            )
            # The CI bound itself scales; only its SIGN is what the gate reads.
            assert (other["significance"]["sharpe_ci_low"] > 0) is (
                base["significance"]["sharpe_ci_low"] > 0
            )


class TestBacktestFlags:
    def test_only_the_annualisation_moves(self):
        assert backtest_flags(12) == PRODUCTION_BACKTEST_FLAGS
        twice_monthly = backtest_flags(24)
        assert twice_monthly[twice_monthly.index("--periods-per-year") + 1] == "24"
        assert set(twice_monthly) - set(PRODUCTION_BACKTEST_FLAGS) == {"24"}
        assert len(twice_monthly) == len(PRODUCTION_BACKTEST_FLAGS)

    def test_periods_per_year_cannot_change_a_verdict(self, tmp_path):
        panel = _planted_panel(n_names=80, n_months=14, rho=0.6, seed=11)
        monthly = evaluate_seed_panel(
            panel, tmp_path / "ppy", "m12", flags=backtest_flags(12)
        )
        twice = evaluate_seed_panel(
            panel, tmp_path / "ppy", "m24", flags=backtest_flags(24)
        )
        assert (
            twice["significance"]["significant"]
            is monthly["significance"]["significant"]
        )
        assert twice["significance"]["deflated_sharpe"] == pytest.approx(
            monthly["significance"]["deflated_sharpe"], abs=1e-9
        )


class TestSmallestDetectableIC:
    def test_picks_the_lowest_ic_over_the_threshold(self):
        results = [
            _fake_result(0.01, 47, 650, label="A", regime="r", gate_pass_rate=0.10),
            _fake_result(0.02, 47, 650, label="A", regime="r", gate_pass_rate=0.55),
            _fake_result(0.03, 47, 650, label="A", regime="r", gate_pass_rate=0.90),
        ]
        assert smallest_detectable_ic(results) == 0.02

    def test_returns_none_when_nothing_is_detectable(self):
        """None must not silently become "the top of the grid"."""
        results = [
            _fake_result(ic, 47, 650, label="A", regime="r", gate_pass_rate=0.2)
            for ic in (0.01, 0.02, 0.03, 0.05)
        ]
        assert smallest_detectable_ic(results) is None

    def test_ignores_the_null_cell(self):
        results = [
            _fake_result(0.0, 47, 650, label="A", regime="r", gate_pass_rate=1.0),
            _fake_result(0.05, 47, 650, label="A", regime="r", gate_pass_rate=0.9),
        ]
        assert smallest_detectable_ic(results) == 0.05


class TestBandReport:
    def _results(self) -> list[CellResult]:
        rates = {
            ("A", 0.01): 0.05,
            ("A", 0.02): 0.20,
            ("A", 0.03): 0.40,
            ("A", 0.05): 0.85,
            ("D", 0.01): 0.10,
            ("D", 0.02): 0.35,
            ("D", 0.03): 0.65,
            ("D", 0.05): 1.00,
        }
        results = [
            _fake_result(0.0, 47, 650, label="A", regime="smallcap", gate_pass_rate=0.02, n_seeds=40),
            _fake_result(0.0, 47, 1000, label="D", regime="smallcap", gate_pass_rate=0.00, n_seeds=40),
        ]
        for (band, ic), rate in rates.items():
            results.append(
                _fake_result(
                    ic,
                    47,
                    650 if band == "A" else 1000,
                    label=band,
                    regime="smallcap",
                    gate_pass_rate=rate,
                )
            )
            # A paired contrast cell, uniformly easier, so the regime
            # paragraph has something real to report.
            results.append(
                _fake_result(
                    ic,
                    47,
                    650 if band == "A" else 1000,
                    label=band,
                    regime="baseline",
                    gate_pass_rate=min(1.0, rate + 0.2),
                )
            )
        return results

    def _render(self) -> str:
        return render_band_markdown(
            _band_manifest(),
            self._results(),
            date_tag="2026-08-05",
            periods_per_year=24,
            primary_regime="smallcap",
            primary_periods=47,
        )

    def test_states_the_detectable_ic_and_the_null_reading_per_band(self):
        body = self._render()
        assert "### Band A — 650 names per period" in body
        assert "### Band D — 1000 names per period" in body
        # A detects at 0.05 (0.85); D at 0.03 (0.65).
        assert "rules out a persistent rank IC at or above 0.05" in body
        assert "rules out a persistent rank IC at or above 0.03" in body
        # And says what a null does NOT prove, naming the largest missed IC.
        assert "does not rule out an IC of 0.03" in body  # band A missed 0.03
        assert "does not rule out an IC of 0.02" in body  # band D missed 0.02

    def test_reports_the_false_positive_rate_per_band(self):
        body = self._render()
        assert "## False-positive rate at planted IC = 0" in body
        assert "False-positive rate at planted IC = 0: **0.02** over 40" in body
        assert "False-positive rate at planted IC = 0: **0.00** over 40" in body

    def test_says_plainly_how_scale_reaches_the_gate(self):
        body = self._render()
        assert "exactly** invariant to any positive rescaling" in body
        assert "net_spread = gross_spread - cost_rate * turnover" in body
        assert "mechanically helps detection" in body
        # And that the direction flatters a small-cap result, which is the
        # part a reader must not miss.
        assert "points the opposite way from the truth" in body
        assert "declared brackets, not estimates" in body

    def test_refuses_to_overstate_an_undetectable_band(self):
        results = [
            _fake_result(0.0, 47, 650, label="A", regime="smallcap", gate_pass_rate=0.0),
            *(
                _fake_result(ic, 47, 650, label="A", regime="smallcap", gate_pass_rate=0.1)
                for ic in (0.01, 0.02, 0.03, 0.05)
            ),
        ]
        body = render_band_markdown(
            _band_manifest(),
            results,
            date_tag="2026-08-05",
            periods_per_year=24,
            primary_regime="smallcap",
            primary_periods=47,
        )
        assert "none in the tested range" in body
        assert "proves nothing about ICs up to 0.05" in body
        assert "UNDERPOWERED" in body
        assert "rules out a persistent rank IC" not in body

    def test_decomposes_the_regime_effect_pairwise(self):
        body = self._render()
        assert "| from | to | axes that move | shared cells |" in body
        # The fixture's contrast cells are uniformly easier, so the gap from
        # the primary to the contrast is positive and the table must say so
        # with the axes that actually moved between the two specs.
        assert "| `baseline` | `smallcap` |" in body
        assert "The largest single effect is" in body

    def test_a_single_regime_grid_refuses_to_quantify_the_regime(self):
        results = [
            _fake_result(ic, 47, 650, label="A", regime="smallcap", gate_pass_rate=0.5)
            for ic in (0.0, 0.02, 0.05)
        ]
        body = render_band_markdown(
            _band_manifest(),
            results,
            date_tag="2026-08-05",
            periods_per_year=24,
            primary_regime="smallcap",
            primary_periods=47,
        )
        assert "cannot say how much of its answer comes from the return process" in body

    def test_names_the_earlier_artifact_as_untouched(self):
        body = self._render()
        assert "power_table_2026-08-03" in body
        assert "does not replace, amend or correct" in body


class TestBandArtifacts:
    def test_band_grid_writes_the_banded_schema_and_reading(self, tmp_path):
        results = TestBandReport()._results()
        written = write_artifacts(
            tmp_path / "out",
            "2026-08-05",
            _band_manifest(),
            results,
            stem="power_table_bands",
            periods_per_year=24,
            primary_regime="smallcap",
            primary_periods=47,
        )
        names = sorted(path.name for path in written)
        assert names == [
            "power_table_bands_2026-08-05.json",
            "power_table_bands_2026-08-05.md",
            "power_table_bands_2026-08-05.sha256",
        ]
        payload = json.loads(
            (tmp_path / "out" / "power_table_bands_2026-08-05.json").read_text()
        )
        assert payload["schema_version"] == "stock_grader.power_table/2"
        assert payload["evaluator"]["flags"][:2] == ["--periods-per-year", "24"]
        assert payload["reading"]["smallest_detectable_ic"] == {"A": 0.05, "D": 0.03}
        assert payload["reading"]["false_positive_rate"] == {"A": 0.02, "D": 0.0}
        assert payload["reading"]["primary_periods"] == 47
        # The sha256 manifest must cover both emitted documents.
        sha_lines = (
            (tmp_path / "out" / "power_table_bands_2026-08-05.sha256")
            .read_text()
            .splitlines()
        )
        assert len(sha_lines) == 2
        for line in sha_lines:
            digest, name = line.split("  ")
            assert (
                hashlib.sha256((tmp_path / "out" / name).read_bytes()).hexdigest()
                == digest
            )

    def test_a_different_stem_does_not_collide_with_the_existing_table(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "power_table_2026-08-05.md").write_text("the earlier artifact")
        results = TestBandReport()._results()
        write_artifacts(
            out_dir,
            "2026-08-05",
            _band_manifest(),
            results,
            stem="power_table_bands",
            periods_per_year=24,
        )
        assert (out_dir / "power_table_2026-08-05.md").read_text() == (
            "the earlier artifact"
        )
        assert (out_dir / "power_table_bands_2026-08-05.md").exists()

    def test_an_unlabelled_grid_still_renders_the_original_report(self, tmp_path):
        results = [
            CellResult(
                planted_ic=0.0,
                months=12,
                universe=250,
                file="panel.parquet",
                input_sha256="0" * 64,
                n_seeds=20,
                gate_pass_rate=0.0,
                dsr_pass_rate=0.0,
                ci_low_positive_rate=0.05,
                mean_dsr=0.4,
                mean_realized_rank_ic=0.001,
                insufficient_sample_verdicts=0,
            )
        ]
        manifest = {
            "artifact": "test grid",
            "created_utc": "2026-01-01T00:00:00+00:00",
            "code_commit": "deadbeef",
            "synthetic_only": True,
        }
        write_artifacts(tmp_path / "out", "1999-01-01", manifest, results)
        body = (tmp_path / "out" / "power_table_1999-01-01.md").read_text()
        assert body.startswith("# Planted-IC power table")
        assert "How to read the September 2026 verdicts" in body
        payload = json.loads(
            (tmp_path / "out" / "power_table_1999-01-01.json").read_text()
        )
        assert payload["schema_version"] == "stock_grader.power_table/1"
        assert "reading" not in payload


class TestExistingArtifactIsUntouched:
    """The 2026-08-03 trio is immutable; this branch may not have moved it."""

    def test_recorded_hashes_still_match_the_files(self):
        directory = REPO_ROOT / "docs" / "calibration"
        manifest = (directory / "power_table_2026-08-03.sha256").read_text()
        for line in manifest.splitlines():
            digest, name = line.split("  ")
            assert (
                hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
            ), name
