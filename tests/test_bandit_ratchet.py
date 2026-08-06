"""The low-severity bandit ratchet must actually ratchet.

An advisory step that cannot fail protects nothing, so the value of this
ratchet is entirely in the two directions it refuses: a rule whose count grew,
and a rule whose count shrank without the baseline being tightened. Both are
asserted here against a stubbed scan, so the tests neither shell out to bandit
nor depend on the repository's current findings.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
from scripts import bandit_ratchet


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    """Point the ratchet at a throwaway baseline of B101=2, B603=1."""
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"total": 3, "per_rule": {"B101": 2, "B603": 1}}))
    monkeypatch.setattr(bandit_ratchet, "BASELINE_PATH", path)
    return path


def _run(monkeypatch, counts: dict[str, int], argv: list[str]) -> int:
    monkeypatch.setattr(bandit_ratchet, "current_counts", lambda: Counter(counts))
    monkeypatch.setattr("sys.argv", ["bandit_ratchet.py", *argv])
    return bandit_ratchet.main()


def test_unchanged_counts_pass(baseline, monkeypatch):
    assert _run(monkeypatch, {"B101": 2, "B603": 1}, []) == 0


def test_a_rule_that_grows_fails(baseline, monkeypatch, capsys):
    assert _run(monkeypatch, {"B101": 3, "B603": 1}, []) == 1
    assert "B101: 2 -> 3" in capsys.readouterr().out


def test_a_brand_new_rule_fails(baseline, monkeypatch, capsys):
    # Absent from the baseline entirely, so .get() must default it to zero
    # rather than skipping the rule.
    assert _run(monkeypatch, {"B101": 2, "B603": 1, "B311": 1}, []) == 1
    assert "B311: 0 -> 1" in capsys.readouterr().out


def test_a_swap_that_holds_the_total_still_fails(baseline, monkeypatch, capsys):
    # Total stays 3. Only per-rule accounting catches this, which is why the
    # baseline records rules and not just a count.
    assert _run(monkeypatch, {"B101": 1, "B603": 2}, []) == 1
    assert "B603: 1 -> 2" in capsys.readouterr().out


def test_a_reduction_demands_the_baseline_be_tightened(baseline, monkeypatch, capsys):
    assert _run(monkeypatch, {"B101": 1, "B603": 1}, []) == 1
    assert "tighten" in capsys.readouterr().out.lower()


def test_update_rewrites_the_baseline(baseline, monkeypatch):
    assert _run(monkeypatch, {"B101": 1, "B603": 1}, ["--update"]) == 0
    written = json.loads(baseline.read_text())
    assert written["total"] == 2
    assert written["per_rule"] == {"B101": 1, "B603": 1}


def test_the_committed_baseline_is_well_formed():
    payload = json.loads(bandit_ratchet.BASELINE_PATH.read_text())
    assert payload["total"] == sum(payload["per_rule"].values())
    assert all(count > 0 for count in payload["per_rule"].values())
