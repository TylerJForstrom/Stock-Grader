from __future__ import annotations

import json
from argparse import Namespace
from datetime import date

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
    rows = []
    for month in (1, 2):
        for index in range(10):
            rows.append(
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
            )
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
    )

    assert cli.cmd_backtest(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(payload["input_contract"].values())
    assert payload["periods"][0]["top_turnover"] == 1.0

    frame = pd.read_csv(path).drop(columns=["universe_is_pit"])
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="strict input contract"):
        cli.cmd_backtest(args)
