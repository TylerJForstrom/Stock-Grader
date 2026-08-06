"""The ADV band's ``reportable`` flag, from the vault's manifest to a refusal.

Stock-Vault computes a §1.3 verdict for every ADV band it exports — a band with
fewer than the pre-registered number of evaluable periods is written but marked
NOT REPORTABLE — and writes it onto the observation dataset's manifest. The flag
had **no downstream consumer**: ``write_signal_panel`` copied the spec keys and
dropped ``adv_band``, so the evaluable panel that the evaluator actually opens
carried no band identity, no floor and no verdict. A band the program refuses
was byte-indistinguishable from one it permits, and its numbers would have been
reported as a band result.

This file is the proof the flag now travels the whole way: manifest -> result ->
``build.json`` and ``manifest.json`` -> report -> a refusal with a non-zero exit
code. The last test is the regression that matters just as much: a panel with no
band metadata must produce the artifacts it always did, byte-for-byte in shape,
so nothing downstream can read an absent key as a passing verdict.
"""

from __future__ import annotations

import datetime as dt
import json
from argparse import Namespace
from pathlib import Path

import pandas as pd
import pytest
from tests.test_signal_panel import (
    SIGNAL,
    _build,
    _build_vault_root,
    _hazard_rows,
    _write_observations,
)

from stock_grader import cli
from stock_grader.backtest import BacktestConfig, backtest_to_markdown, evaluate_walk_forward
from stock_grader.signal_panel import band_report

SIGNAL_2 = dt.date(2026, 8, 10)
ENTRY_2 = dt.date(2026, 8, 11)
EXIT_2 = dt.date(2026, 8, 14)

#: docs/SMALLCAP-PROGRAM.md's own sha256, as the vault stamps it.
PREREG_SHA = "d55bdbfcc18bd24ba748532eb81113b02522f105025ff347367e52052599d483"


@pytest.fixture()
def vault_root(tmp_path: Path) -> Path:
    """The same synthetic archive test_signal_panel builds: bars, the delisted
    cohort and one month of dividends. Declared here rather than imported, so
    the fixture is not also a module-level name this file shadows."""
    return _build_vault_root(tmp_path / "vault")


def _band_block(
    *,
    band_id: str = "A",
    floor: int | None = 1,
    evaluable_periods: int = 47,
    reportable: bool = True,
) -> dict:
    """The shape ``stock_vault.panels._band_report`` writes, trimmed to the keys
    this side reads. ``floor`` is deliberately a fixture parameter: the floor is
    the PRODUCER's declaration, and the grader must adopt it rather than
    hardcode 30 — a program that re-cut its floor and a grader that did not
    would disagree silently."""
    block = {
        "band": {
            "band_id": band_id,
            "adv_floor": 1_000_000.0,
            "adv_cap": 4_000_000.0,
            "label": "illiquid",
            "is_control": False,
        },
        "preregistration": "docs/SMALLCAP-PROGRAM.md",
        "preregistration_sha256": PREREG_SHA,
        "adv_lookback": 20,
        "min_names_per_period": 200,
        "evaluable_periods": evaluable_periods,
        "reportable": reportable,
        "refused_thin_periods": {},
    }
    if floor is not None:
        block["min_evaluable_periods"] = floor
    return block


def _two_period_rows() -> dict[dt.date, list[dict]]:
    return {
        SIGNAL: _hazard_rows(),
        SIGNAL_2: _hazard_rows(signal=SIGNAL_2, entry=ENTRY_2, exit_=EXIT_2),
    }


def _sidecars(panel_path: Path) -> tuple[dict, dict]:
    directory = panel_path.parent
    return (
        json.loads((directory / "build.json").read_text()),
        json.loads((directory / "manifest.json").read_text()),
    )


# -- propagation ---------------------------------------------------------------


def test_band_identity_and_verdict_reach_both_evaluable_sidecars(vault_root):
    """The defect, directly: band metadata must survive write_signal_panel."""
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=1)},
    )
    result, panel_path = _build(vault_root)
    build, manifest = _sidecars(panel_path)

    assert build["adv_band"] == manifest["adv_band"], (
        "the sidecar and the catalog must publish one verdict; a reader that "
        "opens either one must not see a different answer"
    )
    band = build["adv_band"]
    # Identity: which band, and the dollar edges that define it. Without these
    # the panel cannot even be attributed to a band after the fact.
    assert band["band_id"] == "A"
    assert band["band"]["adv_floor"] == 1_000_000.0
    assert band["band"]["adv_cap"] == 4_000_000.0
    assert band["band"]["label"] == "illiquid"
    assert band["band"]["is_control"] is False
    # The floor, the count, and the verdict.
    assert band["min_evaluable_periods"] == 1
    assert band["evaluable_periods"] == result.periods_in_panel == 2
    assert band["reportable"] is True
    assert band["not_reportable_because"] == []
    # Provenance: what the panel was pre-registered under, and the producer's
    # own block verbatim beside the grader's derived verdict.
    assert band["preregistration_sha256"] == PREREG_SHA
    assert band["observations"]["reportable"] is True
    assert band["observations"]["min_names_per_period"] == 200


def test_the_floor_comes_from_the_producer_not_from_this_repo(vault_root):
    """Two periods clear a floor of 1 and fail a floor of 30 — same panel."""
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=30)},
    )
    _, panel_path = _build(vault_root)
    band = _sidecars(panel_path)[0]["adv_band"]
    assert band["min_evaluable_periods"] == 30
    assert band["evaluable_periods"] == 2
    assert band["reportable"] is False
    assert any("evaluable panel has 2 period(s)" in why for why in band["not_reportable_because"])


def test_an_upstream_refusal_is_not_overturned_by_a_healthy_return_join(vault_root):
    """The verdict is the AND of both artifacts, not the grader's leg alone."""
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        # The grader's own leg passes (floor 1, two periods); the vault says the
        # observation panel is thin. A verdict computed only on this side would
        # publish reportable: True and quietly launder the refusal.
        extra={"adv_band": _band_block(floor=1, evaluable_periods=12, reportable=False)},
    )
    _, panel_path = _build(vault_root)
    band = _sidecars(panel_path)[0]["adv_band"]
    assert band["evaluable_periods"] == 2  # this panel's own leg cleared
    assert band["reportable"] is False
    assert any(
        "observation panel is NOT REPORTABLE: 12" in why for why in band["not_reportable_because"]
    )


def test_a_band_with_no_declared_floor_is_refused_not_waved_through(vault_root):
    """A missing floor is missing evidence, never a passing verdict."""
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=None)},
    )
    _, panel_path = _build(vault_root)
    band = _sidecars(panel_path)[0]["adv_band"]
    assert band["min_evaluable_periods"] is None
    assert band["reportable"] is False
    assert any("declares no min_evaluable_periods" in why for why in band["not_reportable_because"])


def test_an_incremental_run_that_prices_nothing_still_republishes_the_verdict(vault_root):
    """The verdict is a property of the panel, not of the run that touched it."""
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=30)},
    )
    _, panel_path = _build(vault_root)
    first = _sidecars(panel_path)[0]["adv_band"]
    result, panel_path = _build(vault_root)  # every part already built: rollup only
    assert result.parts_written == 0
    second = _sidecars(panel_path)[0]["adv_band"]
    assert second == first
    assert second["reportable"] is False


# -- the regression: an unbanded panel is untouched -----------------------------


def test_an_unbanded_panel_carries_no_band_key_at_all(vault_root):
    """No band metadata in, no band key out — in either sidecar.

    An absent key must stay absent rather than becoming ``"adv_band": null`` or
    a default-True verdict: every consumer of these artifacts distinguishes "not
    a band" from "a band that passed", and only a missing key says the first.
    """
    _write_observations(vault_root, "unit_signal", _two_period_rows())
    result, panel_path = _build(vault_root)
    build, manifest = _sidecars(panel_path)
    assert result.adv_band is None
    assert band_report(result) is None
    assert "adv_band" not in build
    assert "adv_band" not in manifest
    # Everything else the sidecar always carried is still there.
    assert build["attestations"]
    assert build["periods_in_panel"] == 2


# -- the evaluator ------------------------------------------------------------


#: Two buckets over ten names a period — the smallest panel that produces a
#: spread at all, so these tests are about the band block and nothing else.
SMALL = BacktestConfig(quantiles=2, min_cross_section=10, bootstrap_samples=0)


def _score_panel() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_date": f"2025-{month:02d}-25",
                "filed_through": f"2025-{month:02d}-25",
                "return_start": f"2025-{month:02d}-26",
                "return_end": f"2025-{month + 1:02d}-25",
                "ticker": f"T{index}",
                "cik": f"{index + 1:010d}",
                "score": index,
                "forward_return": index / 1_000,
                "universe_is_pit": True,
                "return_is_total": True,
                "delisting_return_included": True,
            }
            for month in (1, 2, 3)
            for index in range(10)
        ]
    )


def test_a_refused_band_leads_the_report_and_changes_no_number():
    """The refusal is a heading and a first limitation, not a recomputation."""
    panel = _score_panel()
    baseline = evaluate_walk_forward(panel, SMALL)
    refused = evaluate_walk_forward(
        panel, SMALL, adv_band=band_report_from(_band_block(floor=30, reportable=False), periods=2)
    )
    assert refused.mean_net_spread == baseline.mean_net_spread
    assert refused.mean_rank_ic == baseline.mean_rank_ic
    assert refused.adv_band is not None and refused.adv_band["reportable"] is False
    assert refused.limitations[0].startswith("NOT REPORTABLE: ADV band A")
    assert refused.limitations[1:] == baseline.limitations

    markdown = backtest_to_markdown(refused)
    assert "## ADV band A — NOT REPORTABLE — REFUSED" in markdown
    assert "$1,000,000 to $4,000,000" in markdown
    assert "not a band result" in markdown


def test_a_reportable_band_is_described_without_a_refusal():
    panel = _score_panel()
    baseline = evaluate_walk_forward(panel, SMALL)
    ok = evaluate_walk_forward(
        panel, SMALL, adv_band=band_report_from(_band_block(floor=1), periods=47)
    )
    assert ok.limitations == baseline.limitations
    markdown = backtest_to_markdown(ok)
    assert "## ADV band A — REPORTABLE" in markdown
    assert "NOT REPORTABLE" not in markdown


def test_an_unbanded_report_renders_exactly_as_before():
    report = evaluate_walk_forward(_score_panel(), SMALL)
    assert report.adv_band is None
    assert "ADV band" not in backtest_to_markdown(report)


def band_report_from(observed: dict, *, periods: int) -> dict:
    """The grader-side block, composed the way write_signal_panel composes it."""
    from stock_grader.signal_panel import SignalPanelResult

    result = SignalPanelResult(signal="unit_signal")
    result.adv_band = observed
    result.periods_in_panel = periods
    block = band_report(result)
    assert block is not None
    return block


# -- the CLI verb: a refusal with a non-zero exit code -------------------------


def _backtest_args(panel_path: Path, ledger: Path, **overrides) -> Namespace:
    args = Namespace(
        panel=str(panel_path),
        quantiles=2,
        min_cross_section=10,
        periods_per_year=24,
        transaction_cost_bps=10.0,
        bootstrap_samples=0,
        bootstrap_block_periods=1,
        seed=0,
        # The fixture panel drops LOSTCO as unresolved, so universe_is_pit is
        # honestly False; that is a different refusal from the one under test.
        allow_unverified_panel=True,
        allow_unmanifested_panel=False,
        allow_mixed_universes=False,
        format="json",
        ledger=str(ledger),
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_the_verb_refuses_a_sub_floor_band_and_burns_no_trial(vault_root, tmp_path, capsys):
    """Exit 2, nothing evaluated, and the ledger denominator untouched.

    A refused band produces no statistic, so it must not consume a trial
    either: charging the multiplicity budget for a run that reported nothing
    would deflate every later result for evidence that never existed.
    """
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=30)},
    )
    _, panel_path = _build(vault_root)
    ledger = tmp_path / "ledger.jsonl"

    assert cli.cmd_backtest(_backtest_args(panel_path, ledger)) == 2
    output = capsys.readouterr().out
    assert "NOT REPORTABLE" in output
    assert not ledger.exists(), "a refused run must not extend the append-only ledger"


def test_the_override_evaluates_but_says_so_in_the_report_and_the_ledger(
    vault_root, tmp_path, capsys
):
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=30)},
    )
    _, panel_path = _build(vault_root)
    ledger = tmp_path / "ledger.jsonl"

    assert cli.cmd_backtest(_backtest_args(panel_path, ledger, allow_unreportable_band=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["adv_band"]["reportable"] is False
    assert payload["limitations"][0].startswith("NOT REPORTABLE: ADV band A")

    from stock_grader.research_manifest import load_manifest

    record = load_manifest(ledger)[-1]
    assert "NOT REPORTABLE" in record["leakage_controls"]


def test_the_verb_evaluates_a_reportable_band_normally(vault_root, tmp_path, capsys):
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=1)},
    )
    _, panel_path = _build(vault_root)
    ledger = tmp_path / "ledger.jsonl"
    assert cli.cmd_backtest(_backtest_args(panel_path, ledger)) == 0
    assert ledger.exists()


def test_an_unbanded_panel_evaluates_exactly_as_before(vault_root, tmp_path, capsys):
    """The regression path: no band metadata, no new behaviour of any kind."""
    _write_observations(vault_root, "unit_signal", _two_period_rows())
    _, panel_path = _build(vault_root)
    ledger = tmp_path / "ledger.jsonl"
    assert cli.cmd_backtest(_backtest_args(panel_path, ledger)) == 0
    output = capsys.readouterr().out
    assert "ADV band" not in output
    assert ledger.exists()


def test_a_tampered_sidecar_is_refused_rather_than_believed(vault_root, tmp_path, capsys):
    """The verdict that decides whether a result may be quoted is hash-checked.

    ``build.json`` is cataloged by the manifest that already vouches for the
    panel bytes. Flipping ``reportable`` to True by hand must not buy a report.
    """
    _write_observations(
        vault_root,
        "unit_signal",
        _two_period_rows(),
        extra={"adv_band": _band_block(floor=30)},
    )
    _, panel_path = _build(vault_root)
    build_path = panel_path.parent / "build.json"
    payload = json.loads(build_path.read_text())
    payload["adv_band"]["reportable"] = True
    payload["adv_band"]["not_reportable_because"] = []
    build_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        cli._load_adv_band(panel_path)
