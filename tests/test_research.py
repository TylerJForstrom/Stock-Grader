from __future__ import annotations

import json

import pandas as pd
import pytest

from stock_grader.peers import select_peers
from stock_grader.research import (
    _desirability_percentile,
    build_research_report,
    research_to_json,
    research_to_markdown,
)
from test_pipeline import _universe


def test_research_bundle_contains_grade_peers_raw_evidence_and_provenance():
    snapshots = _universe(12)
    target = snapshots[0]
    target.name = "Target Test Company"
    target.cik = "0000000001"
    target.sic = "3571"
    target.industry = "Electronic Computers"
    target.meta["price_source"] = "fixture"
    target.meta["price_lower_bound"] = 8.5
    target.meta["price_share_basis_check"] = {"status": "not_contradicted"}
    for candidate in snapshots[1:]:
        candidate.sic = "3572"

    peers, selection = select_peers(target, snapshots[1:], minimum=8, maximum=10)
    report = build_research_report(target, peers, selection)
    encoded = json.loads(research_to_json(report))

    assert encoded["schema_version"] == "1.0"
    assert encoded["company"]["cik"] == "0000000001"
    assert encoded["peer_selection"]["members"] == selection.members
    assert encoded["provenance"]["price_source"] == "fixture"
    assert encoded["provenance"]["price_lower_bound"] == 8.5
    assert encoded["provenance"]["price_share_basis_check"]["status"] == "not_contradicted"
    assert encoded["metrics"]
    assert {"raw_value", "normalized_score", "metric_weight", "contribution"} <= set(
        encoded["metrics"][0]
    )
    assert encoded["valuation"]["assumptions"]["interpretation"].endswith(
        "not_analyst_forecasts"
    )


def test_research_markdown_is_complete_and_truthfully_labeled():
    snapshots = _universe(10)
    target = snapshots[0]
    target.sic = "7372"
    for candidate in snapshots[1:]:
        candidate.sic = "7379"
    peers, selection = select_peers(target, snapshots[1:], minimum=8)

    report = build_research_report(target, peers, selection)
    markdown = research_to_markdown(report)

    assert "quantitative research dossier" in markdown
    assert "Company snapshot" in markdown
    assert "Comparable companies" in markdown
    assert "Data provenance" in markdown
    assert "model sensitivity range" in markdown
    assert "not investment advice" in markdown
    assert "Scenario growth rates are assumptions, not forecasts" in markdown
    assert all(
        f"| {metric.name} |" in markdown
        for metric in report.metrics
    )


def test_peer_percentiles_are_oriented_as_desirability():
    peers = pd.Series([10.0, 20.0, 30.0])

    assert _desirability_percentile(
        5.0, peers, direction=-1, ideal_band=None
    ) > 80.0
    assert _desirability_percentile(
        35.0, peers, direction=1, ideal_band=None
    ) > 80.0
    assert _desirability_percentile(
        0.5, pd.Series([0.1, 0.9, 1.2]), direction=0, ideal_band=(0.4, 0.6)
    ) > 80.0


def test_fmt_renders_dimensionless_as_a_multiple_not_a_percentage():
    """A current-ratio-style 1.42 must read 1.42x, never 142.0%.

    Sixty-nine metrics registered unit="ratio"; ten of them (Sharpe, Sortino,
    Calmar, tail ratio, variance ratio, price/SMA200, Graham number, …) are
    dimensionless multiples that percent rendering misstates by 100x.
    """
    from stock_grader.research import _fmt

    assert _fmt(1.42, "dimensionless") == "1.42x"
    assert _fmt(0.85, "dimensionless") == "0.85x"
    assert _fmt(1.42, "ratio") == "142.0%"  # genuine fractions keep percent
    assert _fmt(None, "dimensionless") == "—"


def test_sharpe_like_metrics_are_registered_dimensionless():
    import stock_grader.metrics  # noqa: F401 -- populate the registry
    from stock_grader.registry import METRICS

    units = {name: METRICS.get(name).unit for name in METRICS.names()}
    for name in (
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "tail_ratio",
        "variance_ratio",
        "price_to_sma200",
        "graham_number",
    ):
        assert units[name] == "dimensionless", f"{name} renders as a percentage"
    # And the genuine fractions did not get swept along.
    for name in ("gross_margin", "dividend_yield", "max_drawdown", "annualized_volatility"):
        assert units[name] == "ratio", f"{name} should stay percent-rendered"


def test_metric_evidence_reconciles_exactly_with_the_grade_explain_payload():
    """Every dossier contribution must be the one the grade was computed from.

    The old path re-ran build_metric_matrix and reconstructed contributions from
    pillar internals when a metric was absent from `metric_contributions`,
    mixing exact and approximated numbers in one table.
    """
    snapshots = _universe(12)
    target = snapshots[0]
    peers, selection = select_peers(target, snapshots[1:], minimum=8, maximum=10)
    report = build_research_report(target, peers, selection)

    explained = report.grade.explain["metrics"]
    for row in report.metrics:
        detail = explained.get(row.name)
        assert detail is not None, f"{row.name} missing from the explain payload"
        assert row.contribution == pytest.approx(
            float(detail.get("contribution", 0.0) or 0.0)
        ), f"{row.name} contribution diverges from the grade's own attribution"
        assert row.effective_pillar_weight == pytest.approx(
            float(detail.get("effective_pillar_weight", 0.0) or 0.0)
        )
    # The dossier's total attribution is the grade's, not a lookalike.
    assert sum(r.contribution for r in report.metrics) == pytest.approx(
        sum(float(d.get("contribution", 0.0) or 0.0) for d in explained.values())
    )
