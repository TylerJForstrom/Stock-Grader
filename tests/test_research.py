from __future__ import annotations

import json

import pandas as pd

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
    for candidate in snapshots[1:]:
        candidate.sic = "3572"

    peers, selection = select_peers(target, snapshots[1:], minimum=8, maximum=10)
    report = build_research_report(target, peers, selection)
    encoded = json.loads(research_to_json(report))

    assert encoded["schema_version"] == "1.0"
    assert encoded["company"]["cik"] == "0000000001"
    assert encoded["peer_selection"]["members"] == selection.members
    assert encoded["provenance"]["price_source"] == "fixture"
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
