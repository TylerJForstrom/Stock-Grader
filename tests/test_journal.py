"""Run journal: append-only records, baseline resolution, and honest diffs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from stock_grader import journal
from stock_grader.pipeline import GradeConfig, config_fingerprint
from stock_grader.types import GradeReport, PillarScore, SecuritySnapshot

CONFIG_FP = "c" * 64
UNIVERSE_FP = "u" * 64
WHEN = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def _report(
    ticker: str,
    score: float,
    letter: str,
    *,
    config_fp: str = CONFIG_FP,
    universe_fp: str = UNIVERSE_FP,
    cik: str | None = "0000000001",
    asof: date = date(2026, 8, 1),
    percentile: float | None = 60.0,
    contributions: dict[str, float] | None = None,
    pillar_scores: dict[str, float] | None = None,
) -> GradeReport:
    return GradeReport(
        ticker=ticker,
        asof=asof,
        profile="all_weather",
        score=score,
        letter=letter,
        percentile=percentile,
        coverage=0.9,
        pillars={
            name: PillarScore(pillar=name, score=value)
            for name, value in (pillar_scores or {}).items()
        },
        explain={"metric_contributions": contributions or {}},
        meta={
            "cik": cik,
            "sector": "general",
            "config_fingerprint": config_fp,
            "universe_fingerprint": universe_fp,
        },
    )


def _run(tmp_path: Path, *reports: GradeReport, when: datetime = WHEN) -> Path:
    return journal.append_run(
        {report.ticker: report for report in reports},
        journal_dir=tmp_path,
        recorded_at=when,
    )


class TestAppend:
    def test_append_writes_a_complete_record(self, tmp_path: Path) -> None:
        path = _run(tmp_path, _report("AAPL", 71.2, "B+", contributions={"roe": 2.0}))
        record = journal.load_run(path)
        assert record["schema_version"] == journal.SCHEMA_VERSION
        assert record["command"] == "grade"
        assert record["asof"] == "2026-08-01"
        assert record["profile"] == "all_weather"
        assert record["config_fingerprint"] == CONFIG_FP
        assert record["universe_fingerprint"] == UNIVERSE_FP
        assert record["members"] == [["AAPL", "0000000001", "general"]]
        assert record["membership_fingerprint"] == journal.membership_fingerprint(record["members"])
        report = record["reports"]["AAPL"]
        assert report["letter"] == "B+"
        assert report["score"] == pytest.approx(71.2)
        assert report["explain"]["metric_contributions"] == {"roe": 2.0}

    def test_append_never_overwrites_on_collision(self, tmp_path: Path) -> None:
        first = _run(tmp_path, _report("AAPL", 71.2, "B+"))
        original = first.read_text()
        second = _run(tmp_path, _report("AAPL", 10.0, "F"))
        assert first != second
        assert first.read_text() == original, "existing record was rewritten"
        assert len(list(tmp_path.glob("*.json"))) == 2
        # Timestamped names order the journal chronologically.
        assert sorted([first.name, second.name]) == [first.name, second.name]

    def test_append_refuses_unjournalable_runs(self, tmp_path: Path) -> None:
        with pytest.raises(journal.JournalError, match="empty run"):
            journal.append_run({}, journal_dir=tmp_path)
        bare = GradeReport(ticker="AAPL", asof=date(2026, 8, 1), profile="p", score=1.0, letter="F")
        with pytest.raises(journal.JournalError, match="config_fingerprint"):
            journal.append_run({"AAPL": bare}, journal_dir=tmp_path)
        mixed = {
            "AAPL": _report("AAPL", 70.0, "B"),
            "MSFT": _report("MSFT", 70.0, "B", config_fp="d" * 64),
        }
        with pytest.raises(journal.JournalError, match="mixes config_fingerprint"):
            journal.append_run(mixed, journal_dir=tmp_path)
        assert not list(tmp_path.glob("*.json")), "a refused run must leave no record"

    def test_load_refuses_a_corrupt_record_by_name(self, tmp_path: Path) -> None:
        bad = tmp_path / "20260801T000000000000Z_x.json"
        bad.write_text("{not json")
        with pytest.raises(journal.JournalError, match=bad.name):
            journal.load_run(bad)


class TestMembership:
    def test_snapshot_and_report_members_agree(self) -> None:
        snapshots = [
            SecuritySnapshot(ticker="aapl", asof=date(2026, 8, 1), cik="0000000001"),
            SecuritySnapshot(ticker="MSFT", asof=date(2026, 8, 1), cik=None),
        ]
        reports = {
            "AAPL": _report("AAPL", 70.0, "B"),
            "MSFT": _report("MSFT", 60.0, "B-", cik=None),
        }
        assert journal.snapshot_members(snapshots) == journal.report_members(reports)
        assert journal.membership_fingerprint(
            journal.snapshot_members(snapshots)
        ) == journal.membership_fingerprint(journal.report_members(reports))

    def test_membership_fingerprint_ignores_vintage_but_not_members(self) -> None:
        base = [["AAPL", "1", "general"]]
        assert journal.membership_fingerprint(base) == journal.membership_fingerprint(
            [["AAPL", "1", "general"]]
        )
        assert journal.membership_fingerprint(base) != journal.membership_fingerprint(
            [["AAPL", "2", "general"]]
        )

    def test_config_fingerprint_is_public_and_config_sensitive(self) -> None:
        default = config_fingerprint(GradeConfig())
        assert len(default) == 64 and default == config_fingerprint(GradeConfig())
        assert default != config_fingerprint(GradeConfig(sector_neutral=False))


class TestPreviousLetters:
    def test_newest_matching_run_wins_and_na_is_excluded(self, tmp_path: Path) -> None:
        _run(tmp_path, _report("AAPL", 71.2, "B+"), _report("BAD", float("nan"), "N/A"))
        _run(tmp_path, _report("AAPL", 68.9, "B"), when=LATER)
        members = journal.membership_fingerprint([["AAPL", "0000000001", "general"]])
        both = journal.membership_fingerprint(
            [["AAPL", "0000000001", "general"], ["BAD", "0000000001", "general"]]
        )
        assert journal.previous_letters(
            tmp_path, config_fingerprint=CONFIG_FP, membership_fingerprint=members
        ) == {"AAPL": "B"}
        assert journal.previous_letters(
            tmp_path, config_fingerprint=CONFIG_FP, membership_fingerprint=both
        ) == {"AAPL": "B+"}

    def test_regime_mismatch_or_missing_journal_yields_nothing(self, tmp_path: Path) -> None:
        _run(tmp_path, _report("AAPL", 71.2, "B+"))
        members = journal.membership_fingerprint([["AAPL", "0000000001", "general"]])
        assert (
            journal.previous_letters(
                tmp_path, config_fingerprint="e" * 64, membership_fingerprint=members
            )
            == {}
        )
        assert (
            journal.previous_letters(
                tmp_path, config_fingerprint=CONFIG_FP, membership_fingerprint="f" * 64
            )
            == {}
        )
        assert (
            journal.previous_letters(
                tmp_path / "absent", config_fingerprint=CONFIG_FP, membership_fingerprint=members
            )
            == {}
        )


class TestSinceLast:
    def test_baseline_skips_runs_without_the_ticker(self, tmp_path: Path) -> None:
        oldest = _run(tmp_path, _report("AAPL", 71.2, "B+"))
        _run(tmp_path, _report("MSFT", 50.0, "C"), when=datetime(2026, 8, 2, tzinfo=UTC))
        newest = _run(tmp_path, _report("AAPL", 68.9, "B"), when=LATER)
        (base_path, baseline), (cur_path, current) = journal.resolve_since_last(tmp_path, "aapl")
        assert (base_path, cur_path) == (oldest, newest)
        assert baseline["reports"]["AAPL"]["letter"] == "B+"
        assert current["reports"]["AAPL"]["letter"] == "B"

    def test_refusals_name_the_reason(self, tmp_path: Path) -> None:
        with pytest.raises(journal.JournalError, match="no run journal"):
            journal.resolve_since_last(tmp_path / "absent", "AAPL")
        with pytest.raises(journal.JournalError, match="no journaled run contains AAPL"):
            journal.resolve_since_last(tmp_path, "AAPL")
        _run(tmp_path, _report("AAPL", 71.2, "B+"))
        with pytest.raises(journal.JournalError, match="only one journaled run"):
            journal.resolve_since_last(tmp_path, "AAPL")


class TestComparability:
    def test_vintage_movement_alone_is_comparable(self, tmp_path: Path) -> None:
        baseline = journal.load_run(_run(tmp_path, _report("AAPL", 71.2, "B+")))
        current = journal.load_run(
            _run(
                tmp_path,
                _report("AAPL", 68.9, "B", universe_fp="v" * 64, asof=date(2026, 8, 3)),
                when=LATER,
            )
        )
        assert journal.comparability_mismatches(baseline, current) == []

    def test_config_and_membership_breaks_are_named(self, tmp_path: Path) -> None:
        baseline = journal.load_run(_run(tmp_path, _report("AAPL", 71.2, "B+")))
        config_break = journal.load_run(
            _run(tmp_path, _report("AAPL", 68.9, "B", config_fp="d" * 64), when=LATER)
        )
        (reason,) = journal.comparability_mismatches(baseline, config_break)
        assert "config fingerprint changed" in reason
        membership_break = journal.load_run(
            _run(
                tmp_path,
                _report("AAPL", 68.9, "B"),
                _report("TSLA", 40.0, "D"),
                when=datetime(2026, 8, 4, tzinfo=UTC),
            )
        )
        (reason,) = journal.comparability_mismatches(baseline, membership_break)
        assert "membership changed" in reason and "added TSLA" in reason


class TestDiff:
    def test_deltas_and_movers(self, tmp_path: Path) -> None:
        baseline = journal.load_run(
            _run(
                tmp_path,
                _report(
                    "AAPL",
                    71.2,
                    "B+",
                    percentile=64.0,
                    contributions={"roe": 2.0, "margin": 1.0},
                    pillar_scores={"quality": 60.0, "valuation": 55.0},
                ),
            )
        )
        current = journal.load_run(
            _run(
                tmp_path,
                _report(
                    "AAPL",
                    68.9,
                    "B",
                    asof=date(2026, 8, 3),
                    percentile=58.0,
                    contributions={"roe": -1.0, "margin": 1.2, "buyback_yield": 0.5},
                    pillar_scores={"quality": 55.0, "growth": 55.0},
                ),
                when=LATER,
            )
        )
        diff = journal.diff_reports(baseline, current, "AAPL")
        assert diff["letter"] == {"from": "B+", "to": "B", "changed": True}
        assert diff["score"]["delta"] == pytest.approx(-2.3)
        assert diff["percentile"]["delta"] == pytest.approx(-6.0)
        assert diff["pillars"]["quality"]["delta"] == pytest.approx(-5.0)
        # Pillars present on one side only stay visible instead of vanishing.
        assert diff["pillars"]["valuation"] == {"from": 55.0, "to": None, "delta": None}
        assert diff["pillars"]["growth"] == {"from": None, "to": 55.0, "delta": None}
        movers = diff["metric_movers"]
        assert [entry["metric"] for entry in movers] == ["roe", "buyback_yield", "margin"]
        assert movers[0]["delta"] == pytest.approx(-3.0)
        # A metric absent from the baseline still reports its full appearance.
        assert movers[1] == {
            "metric": "buyback_yield",
            "from": None,
            "to": 0.5,
            "delta": 0.5,
        }
        assert diff["baseline"]["asof"] == "2026-08-01"
        assert diff["current"]["asof"] == "2026-08-03"

    def test_non_finite_scores_diff_as_none(self, tmp_path: Path) -> None:
        baseline = journal.load_run(
            _run(tmp_path, _report("AAPL", float("nan"), "N/A", percentile=None))
        )
        current = journal.load_run(_run(tmp_path, _report("AAPL", 68.9, "B"), when=LATER))
        diff = journal.diff_reports(baseline, current, "AAPL")
        assert diff["score"] == {"from": None, "to": 68.9, "delta": None}
        assert diff["letter"]["changed"] is True
