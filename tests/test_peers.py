from __future__ import annotations

from datetime import timedelta

from stock_grader.peers import select_peers
from stock_grader.types import SectorClass
from test_pipeline import _universe


def test_peer_selection_is_deterministic_and_never_crosses_business_model():
    snapshots = _universe(14)
    target = snapshots[0]
    target.sic = "3571"
    target.sector = SectorClass.GENERAL
    for i, candidate in enumerate(snapshots[1:], 1):
        candidate.sic = "3572" if i < 5 else "3585"
        candidate.sector = SectorClass.GENERAL
    snapshots[-1].sector = SectorClass.BANK
    snapshots[-1].sic = "6021"

    first, manifest = select_peers(target, snapshots[1:], minimum=5, maximum=8)
    second, repeated = select_peers(target, list(reversed(snapshots[1:])), minimum=5, maximum=8)

    assert [item.ticker for item in first] == [item.ticker for item in second]
    assert all(item.sector is SectorClass.GENERAL for item in first)
    assert snapshots[-1].ticker not in manifest.members
    assert "business model" in manifest.excluded[snapshots[-1].ticker]
    assert manifest.fingerprint == repeated.fingerprint


def test_peer_selection_records_widening_and_shortfall():
    snapshots = _universe(4)
    target = snapshots[0]
    target.sic = "7372"
    for candidate in snapshots[1:]:
        candidate.sic = "7379"

    peers, manifest = select_peers(target, snapshots[1:], minimum=8, maximum=10)

    assert len(peers) == 3
    assert manifest.widening_steps
    assert any("only 3 defensible peers" in warning for warning in manifest.warnings)


def test_peer_selection_validates_bounds():
    target = _universe(1)[0]

    try:
        select_peers(target, [], minimum=1)
    except ValueError as exc:
        assert "at least 2" in str(exc)
    else:
        raise AssertionError("minimum=1 should fail")


def test_auto_peers_require_the_same_snapshot_date_and_deduplicate_by_cik():
    snapshots = _universe(5)
    target = snapshots[0]
    target.cik = "1"
    target.sic = "7372"
    for candidate in snapshots[1:]:
        candidate.sic = "7372"
    snapshots[1].asof = target.asof - timedelta(days=1)
    snapshots[2].cik = "22"
    snapshots[3].cik = "22"
    snapshots[3].ticker = "ALIAS"

    _peers, manifest = select_peers(target, snapshots[1:], minimum=2, maximum=4)

    assert snapshots[1].ticker not in manifest.members
    assert "as-of date" in manifest.excluded[snapshots[1].ticker]
    assert sum(identifier == "CIK:0000000022" for identifier in manifest.member_identifiers) == 1
    assert "duplicate security identifier" in manifest.excluded["ALIAS"]
