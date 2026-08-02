from __future__ import annotations

import json
import math
from datetime import date
from io import StringIO

import numpy as np
import pandas as pd
import pytest
from rich.console import Console

from stock_grader.profiles import ConsensusResult
from stock_grader.report import (
    DISCLAIMER,
    rank_reports,
    render_report,
    to_consensus_markdown,
    to_json,
    to_markdown,
    to_ranking_markdown,
)
from stock_grader.types import GradeReport, PillarScore


def _report(
    ticker: str,
    score: float,
    letter: str,
    *,
    probabilities: dict[str, float] | None = None,
) -> GradeReport:
    return GradeReport(
        ticker=ticker,
        asof=date(2026, 7, 28),
        profile="quality",
        score=score,
        letter=letter,
        ci=(score - 5.0, score + 5.0) if math.isfinite(score) else None,
        coverage=0.9,
        weighting_method="equal/fixed",
        normalizer="robust_z",
        aggregator="weighted_mean/ces",
        explain={
            "letter_probabilities": probabilities or {},
            "n_metrics_ok": 9,
            "n_metrics_missing": 1,
            "n_metrics_not_applicable": 0,
        },
        meta={"sector": "general"},
    )


def test_top_contributors_use_effective_weights_and_filter_sign() -> None:
    report = _report("XYZ", 70.0, "B")
    report.pillars = {
        "quality": PillarScore(
            pillar="quality",
            score=70.0,
            contributions={"quality_up": 4.0, "quality_down": -2.0, "neutral": 0.0},
        ),
        "health": PillarScore(
            pillar="health",
            score=60.0,
            contributions={"health_up": 3.0, "health_down": -4.0},
        ),
    }
    report.pillar_weights = {"quality": 0.8, "health": 0.2}
    report.effective_pillar_weights = {"quality": 0.25, "health": 0.75}

    assert report.top_contributors() == [
        ("health_up", pytest.approx(2.25)),
        ("quality_up", pytest.approx(1.0)),
    ]
    assert report.top_contributors(positive=False) == [
        ("health_down", pytest.approx(-3.0)),
        ("quality_down", pytest.approx(-0.5)),
    ]


def test_top_contributors_fall_back_to_nominal_weights_for_legacy_reports() -> None:
    report = _report("XYZ", 70.0, "B")
    report.pillars = {
        "quality": PillarScore(
            pillar="quality",
            score=70.0,
            contributions={"up": 4.0, "down": -2.0},
        )
    }
    report.pillar_weights = {"quality": 0.5}

    assert report.top_contributors() == [("up", pytest.approx(2.0))]
    assert report.top_contributors(positive=False) == [("down", pytest.approx(-1.0))]


def test_consensus_excludes_finite_scores_that_were_not_graded() -> None:
    graded = _report("XYZ", 72.0, "B")
    refused = _report("XYZ", 99.0, "N/A")

    result = ConsensusResult("XYZ", {"quality": graded, "value": refused})

    assert result.score == pytest.approx(72.0)
    assert result.letter == "B"
    assert result.scores.to_dict() == {"quality": 72.0}
    assert set(result.to_dict()["per_profile"]) == {"quality", "value"}


def test_consensus_is_na_when_every_profile_refused() -> None:
    result = ConsensusResult(
        "XYZ",
        {
            "quality": _report("XYZ", 85.0, "N/A"),
            "value": _report("XYZ", 40.0, "N/A"),
        },
    )

    assert result.letter == "N/A"
    assert math.isnan(result.score)
    assert result.scores.empty


def test_consensus_letter_uses_the_same_cross_sectional_scale_as_its_score() -> None:
    lower = _report("XYZ", 79.0, "B")
    upper = _report("XYZ", 81.0, "B+")
    lower.meta["curve"] = "cross_sectional"
    upper.meta["curve"] = "cross_sectional"

    result = ConsensusResult("XYZ", {"quality": lower, "value": upper})

    assert result.score == pytest.approx(80.0)
    assert result.letter == "B+"


def test_report_uses_truthful_sensitivity_wording_and_surfaces_scenario_frequencies() -> None:
    report = _report("XYZ", 70.0, "B", probabilities={"B": 0.6, "B-": 0.3, "C+": 0.1})
    stream = StringIO()
    console = Console(file=stream, width=180, color_system=None, force_terminal=False)

    render_report(report, console)
    terminal = stream.getvalue()
    markdown = to_markdown(report)
    payload = json.loads(to_json(report))

    assert "90% sensitivity" in terminal
    assert "not a return forecast" in terminal
    assert "letter scenario frequencies" in terminal
    assert "90% CI" not in terminal

    assert "90% model sensitivity interval" in markdown
    assert "Letter frequencies under those model perturbations" in markdown
    assert "confidence interval" not in markdown

    assert payload["ci"] == [65.0, 75.0]
    assert payload["sensitivity_interval"] == [65.0, 75.0]
    assert payload["letter_probabilities"] == {"B": 0.6, "B-": 0.3, "C+": 0.1}


def test_json_encoder_emits_strict_portable_json_for_analysis_types() -> None:
    payload = json.loads(
        to_json(
            {
                "scalar": np.int64(7),
                "array": np.asarray([1.0, np.nan]),
                "series": pd.Series({"x": np.float64(2.5)}),
                "timestamp": pd.Timestamp("2026-07-28"),
                "infinity": float("inf"),
            }
        )
    )

    assert payload == {
        "array": [1.0, None],
        "infinity": None,
        "scalar": 7,
        "series": {"x": 2.5},
        "timestamp": "2026-07-28T00:00:00",
    }


def test_ranking_formats_share_order_and_top_limit() -> None:
    reports = {
        "LOW": _report("LOW", 50.0, "C"),
        "REFUSED": _report("REFUSED", 99.0, "N/A"),
        "HIGH": _report("HIGH", 80.0, "A"),
    }

    assert [report.ticker for report in rank_reports(reports, top=2)] == ["HIGH", "LOW"]
    markdown = to_ranking_markdown(reports, top=1)
    assert "| HIGH |" in markdown
    assert "| LOW |" not in markdown
    assert "| REFUSED |" not in markdown


def test_consensus_markdown_names_excluded_profiles() -> None:
    result = ConsensusResult(
        "XYZ",
        {
            "quality": _report("XYZ", 72.0, "B"),
            "value": _report("XYZ", 90.0, "N/A"),
        },
    )

    markdown = to_consensus_markdown({"XYZ": result})

    assert "| XYZ | B | 72.0 |" in markdown
    assert "| value | N/A | 90.0 | excluded (not graded) |" in markdown


def test_markdown_renderers_carry_disclaimer_and_provenance():
    """A pasted report must be self-identifying (ECOSYSTEM rule 3) and framed.

    Two scores are comparable only when their fingerprints match, and a
    markdown grade circulates detached from the run that produced it. The
    ranking table was also the one renderer of the set missing the
    not-investment-advice line (REVISED_PLAN item; AGENTS.md makes it binding).
    """
    report = _report("XYZ", 70.0, "B")
    report.meta["config_fingerprint"] = "a" * 64
    report.meta["universe_fingerprint"] = "b" * 64
    single = to_markdown(report)
    ranking = to_ranking_markdown({"XYZ": report})

    for rendered in (single, ranking):
        assert DISCLAIMER in rendered
        assert "**As of**" in rendered
        assert "**Config**" in rendered and "**Universe**" in rendered
