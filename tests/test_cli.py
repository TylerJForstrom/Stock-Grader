from __future__ import annotations

import json
import math
from argparse import Namespace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from stock_grader import cli
from stock_grader.profiles import ConsensusResult
from stock_grader.types import GradeReport, SecuritySnapshot


def _report(ticker: str, score: float, letter: str) -> GradeReport:
    return GradeReport(
        ticker=ticker,
        asof=date(2026, 7, 28),
        profile="quality",
        score=score,
        letter=letter,
        ci=(score - 5.0, score + 5.0),
        coverage=0.9,
        explain={"letter_probabilities": {letter: 1.0}},
        meta={"sector": "general"},
    )


def _patch_data_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SECClient", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "SECProvider", lambda _client: object())
    monkeypatch.setattr(
        cli,
        "_build_snapshots",
        lambda tickers, _args, *, provider: [
            SecuritySnapshot(ticker=ticker, asof=date(2026, 7, 28)) for ticker in tickers
        ],
    )


def _common_args(**overrides) -> Namespace:
    values = {
        "cache_dir": None,
        "contact": None,
        "no_network": True,
        "profile": "all_weather",
        "weighting": None,
        "normalizer": None,
        "aggregator": None,
        "rho": None,
        "sector_neutral": False,
        "curve": None,
        "format": "text",
    }
    values.update(overrides)
    return Namespace(**values)


def test_rank_json_applies_top_and_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_loading(monkeypatch)
    monkeypatch.setattr(cli, "_load_universe", lambda _path: ["LOW", "HIGH", "REFUSED"])
    monkeypatch.setattr(cli, "_config_from_args", lambda _args: object())
    monkeypatch.setattr(
        cli,
        "grade_universe",
        lambda _snapshots, _config: {
            "LOW": _report("LOW", 50.0, "C"),
            "HIGH": _report("HIGH", 80.0, "A"),
            "REFUSED": _report("REFUSED", 99.0, "N/A"),
        },
    )
    args = _common_args(universe="ignored.txt", format="json", top=1)

    assert cli.cmd_rank(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert list(payload) == ["HIGH"]


def test_rank_markdown_is_a_ranked_table_and_applies_top(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_loading(monkeypatch)
    monkeypatch.setattr(cli, "_load_universe", lambda _path: ["LOW", "HIGH"])
    monkeypatch.setattr(cli, "_config_from_args", lambda _args: object())
    monkeypatch.setattr(
        cli,
        "grade_universe",
        lambda _snapshots, _config: {
            "LOW": _report("LOW", 50.0, "C"),
            "HIGH": _report("HIGH", 80.0, "A"),
        },
    )
    args = _common_args(universe="ignored.txt", format="md", top=1)

    assert cli.cmd_rank(args) == 0
    output = capsys.readouterr().out

    assert "# Ranked universe" in output
    assert "| HIGH |" in output
    assert "| LOW |" not in output


def test_consensus_json_honours_aggregator_and_rho(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_loading(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_peers", lambda _args, _tickers: [])
    captured: dict = {}

    def fake_consensus(snapshots, **overrides):
        captured.update(overrides)
        ticker = snapshots[0].ticker
        return {
            ticker: ConsensusResult(
                ticker,
                {
                    "quality": _report(ticker, 75.0, "B+"),
                    "value": _report(ticker, 90.0, "N/A"),
                },
            )
        }

    monkeypatch.setattr(cli, "consensus_grade", fake_consensus)
    args = _common_args(
        tickers=["XYZ"],
        format="json",
        aggregator="weighted_mean",
        rho=0.25,
    )

    assert cli.cmd_consensus(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert captured["pillar_aggregator"] == "weighted_mean"
    assert captured["aggregator_kwargs"] == {"rho": 0.25}
    assert payload["letter"] == "B+"
    assert payload["scores"] == {"quality": 75.0}
    assert payload["per_profile"]["value"]["letter"] == "N/A"


def test_consensus_markdown_format(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_loading(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_peers", lambda _args, _tickers: [])
    monkeypatch.setattr(
        cli,
        "consensus_grade",
        lambda snapshots, **_overrides: {
            snapshots[0].ticker: ConsensusResult(
                snapshots[0].ticker,
                {"quality": _report(snapshots[0].ticker, 75.0, "B+")},
            )
        },
    )
    args = _common_args(tickers=["XYZ"], format="md")

    assert cli.cmd_consensus(args) == 0
    output = capsys.readouterr().out

    assert "# Consensus across profiles" in output
    assert "| XYZ | B+ | 75.0 |" in output


def test_price_provider_selection_exposes_tiingo_and_validates_conflicts() -> None:
    auto = Namespace(
        price_provider="auto",
        price_dir=None,
        stockanalysis=False,
        no_network=False,
        cache_dir=None,
        contact=None,
        sec_prices=True,
    )
    assert [provider.name for provider in cli._price_providers_from_args(auto)] == [
        "tiingo",
        "yahoo",
    ]

    explicit = Namespace(**{**vars(auto), "price_provider": "tiingo"})
    assert [provider.name for provider in cli._price_providers_from_args(explicit)] == ["tiingo"]

    missing_csv = Namespace(**{**vars(auto), "price_provider": "csv"})
    with pytest.raises(ValueError, match="requires --price-dir"):
        cli._price_providers_from_args(missing_csv)


def test_public_float_lower_bound_is_evidence_not_snapshot_price() -> None:
    snapshot = SecuritySnapshot(ticker="XYZ", asof=date(2026, 7, 28))
    cli._apply_resolved_price(
        snapshot,
        {
            "price": 25.0,
            "source": "public_float_lower_bound",
            "date": date(2026, 6, 30),
            "age_days": 28,
            "non_affiliate_fraction": None,
            "valuation_eligible": False,
        },
    )

    assert snapshot.price is None
    assert snapshot.meta["price_lower_bound"] == pytest.approx(25.0)
    assert snapshot.meta["valuation_price_rejected"] == "public_float_lower_bound"
    assert any("all exact valuation metrics are N/A" in item for item in snapshot.warnings)


def test_historical_yahoo_price_is_quarantined_when_split_basis_is_unverified() -> None:
    snapshot = SecuritySnapshot(
        ticker="XYZ",
        asof=date(2020, 7, 28),
        price=20.0,
    )
    cli._apply_yahoo_basis_gate(
        snapshot,
        {
            "status": "not_contradicted",
            "public_to_total_share_ratio": 1.0,
        },
        historical_asof=True,
        basis_reconciled=False,
    )

    assert snapshot.price is None
    assert snapshot.meta["rejected_dense_price"] == pytest.approx(20.0)
    assert snapshot.meta["valuation_price_rejected"] == "split_basis_unverified"
    assert any("exact valuation metrics are N/A" in item for item in snapshot.warnings)


def test_current_yahoo_price_is_quarantined_without_split_event_evidence() -> None:
    snapshot = SecuritySnapshot(
        ticker="XYZ",
        asof=date(2026, 7, 28),
        price=20.0,
    )
    cli._apply_yahoo_basis_gate(
        snapshot,
        {"status": "not_contradicted"},
        historical_asof=False,
        basis_reconciled=False,
    )

    assert snapshot.price is None
    assert snapshot.meta["valuation_price_rejected"] == "split_basis_unverified"


def test_current_yahoo_split_events_rebase_dated_dei_shares() -> None:
    snapshot = SecuritySnapshot(
        ticker="XYZ",
        asof=date(2026, 7, 28),
        price=20.0,
        shares_outstanding=1e9,
        meta={
            "shares_date": pd.Timestamp("2025-01-15"),
            "shares_history": pd.Series(
                [1e9],
                index=pd.to_datetime(["2025-01-15"]),
            ),
        },
    )
    frame = pd.DataFrame(
        {"close": [18.0, 20.0]},
        index=pd.to_datetime(["2024-12-31", "2026-07-28"]),
    )
    frame.attrs["split_events"] = [
        {"date": "2025-06-01", "factor": 4.0, "ratio": "4:1"}
    ]

    reconciled = cli._reconcile_current_yahoo_share_basis(
        snapshot,
        frame,
        historical_asof=False,
    )

    assert reconciled
    assert snapshot.shares_outstanding == pytest.approx(4e9)
    assert snapshot.meta["shares_split_rebased_factor"] == pytest.approx(4.0)
    assert snapshot.meta["shares_history_price_basis"].iloc[0] == pytest.approx(4e9)
    cli._apply_yahoo_basis_gate(
        snapshot,
        {"status": "not_contradicted"},
        historical_asof=False,
        basis_reconciled=True,
    )
    assert snapshot.price == pytest.approx(20.0)


def test_parser_rejects_non_positive_top_and_accepts_explicit_price_provider() -> None:
    parser = cli.build_parser()

    parsed = parser.parse_args(["grade", "AAPL", "--price-provider", "tiingo"])
    assert parsed.price_provider == "tiingo"

    with pytest.raises(SystemExit):
        parser.parse_args(["rank", "--universe", "tickers.txt", "--top", "0"])


def test_backtest_cli_requires_and_reports_a_verifiable_input_contract(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
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
        for month in (1, 2)
        for index in range(10)
    ]
    path = tmp_path / "panel.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    args = Namespace(
        panel=str(path),
        quantiles=2,
        min_cross_section=10,
        periods_per_year=12,
        transaction_cost_bps=10.0,
        bootstrap_samples=0,
        bootstrap_block_periods=1,
        seed=0,
        allow_unverified_panel=False,
        format="json",
        # Point at a scratch ledger: without this the run appends a junk trial
        # to the repo's real research_ledger.jsonl, deflating every future DSR.
        ledger=str(tmp_path / "ledger.jsonl"),
    )

    assert cli.cmd_backtest(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(payload["input_contract"].values())
    assert payload["periods"][0]["top_turnover"] == 1.0

    frame = pd.read_csv(path).drop(columns=["universe_is_pit"])
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="strict input contract"):
        cli.cmd_backtest(args)


def test_backtest_records_trials_and_deflates_by_ledger_history(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every run is a trial: the ledger accumulates and the DSR deflates by it."""
    rows = [
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
    path = tmp_path / "panel.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    ledger = tmp_path / "ledger.jsonl"

    def run() -> dict:
        args = Namespace(
            panel=str(path), quantiles=2, min_cross_section=10, periods_per_year=12,
            transaction_cost_bps=10.0, bootstrap_samples=0, bootstrap_block_periods=1,
            seed=0, allow_unverified_panel=False, format="json", ledger=str(ledger),
        )
        assert cli.cmd_backtest(args) == 0
        return json.loads(capsys.readouterr().out)

    first = run()
    assert first["ledger"]["lifetime_trials"] == 1
    second = run()
    assert second["ledger"]["lifetime_trials"] == 2

    from stock_grader.research_manifest import load_manifest, verify_line

    records = load_manifest(ledger)
    assert len(records) == 2
    assert all(verify_line(record) for record in records)  # tamper-evident chain
    assert records[-1]["trials"] == 2


def test_backtest_null_sharpe_trial_does_not_poison_later_deflation(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A panel too short for a Sharpe stores JSON null, never a ledger-poisoning NaN."""

    def rows(months: tuple[int, ...]) -> list[dict]:
        return [
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
            for month in months
            for index in range(10)
        ]

    short = tmp_path / "short.csv"
    pd.DataFrame(rows((1,))).to_csv(short, index=False)  # one period: no Sharpe possible
    real = tmp_path / "real.csv"
    pd.DataFrame(rows((1, 2, 3))).to_csv(real, index=False)
    ledger = tmp_path / "ledger.jsonl"

    def run(panel: Path) -> dict:
        args = Namespace(
            panel=str(panel), quantiles=2, min_cross_section=10, periods_per_year=12,
            transaction_cost_bps=10.0, bootstrap_samples=0, bootstrap_block_periods=1,
            seed=0, allow_unverified_panel=False, format="json", ledger=str(ledger),
        )
        assert cli.cmd_backtest(args) == 0
        return json.loads(capsys.readouterr().out)

    run(short)
    first = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert first["metrics"]["per_period_sharpe"] is None  # JSON null, not NaN
    assert first["metrics"]["deflated_sharpe"] is None

    payload = run(real)
    # Old behavior: the NaN trial made stdev(trial_sharpes) NaN, so every later
    # deflation was NaN ("NO EDGE" forever). The null trial must not deflate.
    assert math.isfinite(payload["significance"]["deflated_sharpe"])
    assert payload["ledger"]["lifetime_trials"] == 1  # only the finite-Sharpe trial counts


def test_freeze_writes_immutable_dated_panel(tmp_path, monkeypatch):
    """The forward panel: one parquet per date, never overwritten."""
    from tests.test_pipeline import _universe

    snapshots = _universe(16, with_prices=False)
    monkeypatch.setattr(cli, "_load_universe", lambda path: [s.ticker for s in snapshots])
    monkeypatch.setattr(cli, "_build_snapshots", lambda tickers, args, provider: snapshots)

    args = Namespace(
        out=str(tmp_path / "frozen"), universe="ignored.txt", asof="2026-07-29",
        cache_dir=None, contact=None, no_network=True, profile="all_weather", weighting=None,
        normalizer=None, aggregator=None, rho=None, pit=False, refresh=False,
        sector_neutral=None, curve=None,
    )
    assert cli.cmd_freeze(args) == 0
    out = tmp_path / "frozen" / "2026-07-29.parquet"
    assert out.exists()
    frame = pd.read_parquet(out)
    assert len(frame) == 16
    assert set(frame.columns) >= {
        "signal_date", "ticker", "cik", "score", "letter", "graded",
        "config_fingerprint", "universe_fingerprint", "code_commit",
    }
    before = out.read_bytes()
    assert cli.cmd_freeze(args) == 0  # second run: skip, never overwrite
    assert out.read_bytes() == before


def test_freeze_refuses_a_nearly_ungraded_panel(
    tmp_path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An EDGAR outage that fails every gate must not freeze a tradeable-looking parquet."""
    from tests.test_pipeline import _universe

    snapshots = _universe(16, with_prices=False)
    monkeypatch.setattr(cli, "_load_universe", lambda path: [s.ticker for s in snapshots])
    monkeypatch.setattr(cli, "_build_snapshots", lambda tickers, args, provider: snapshots)
    # Every gate refused a letter: scores exist but nothing is graded.
    monkeypatch.setattr(
        cli,
        "grade_universe",
        lambda _snapshots, _config: {s.ticker: _report(s.ticker, 50.0, "N/A") for s in snapshots},
    )

    args = Namespace(
        out=str(tmp_path / "frozen"), universe="ignored.txt", asof="2026-07-29",
        cache_dir=None, contact=None, no_network=True, profile="all_weather", weighting=None,
        normalizer=None, aggregator=None, rho=None, pit=False, refresh=False,
        sector_neutral=None, curve=None,
    )
    assert cli.cmd_freeze(args) == 2
    assert not (tmp_path / "frozen" / "2026-07-29.parquet").exists()
    assert "refusing to freeze" in capsys.readouterr().out


def test_monthly_freeze_workflow_fails_fast_without_sec_contact() -> None:
    """An unset SEC_CONTACT_EMAIL must abort the freeze, not degrade the SEC User-Agent."""
    workflow = (
        Path(__file__).resolve().parent.parent
        / ".github" / "workflows" / "monthly-freeze.yml"
    ).read_text(encoding="utf-8")

    guard = workflow.find('if [ -z "$STOCK_GRADER_CONTACT" ]')
    invocation = workflow.find("stock-grader freeze")
    assert guard != -1, "freeze step must fail fast when STOCK_GRADER_CONTACT is empty"
    assert guard < invocation
    guard_block = workflow[guard:invocation]
    assert "SEC_CONTACT_EMAIL" in guard_block  # the message names the secret to set
    assert "exit 1" in guard_block
