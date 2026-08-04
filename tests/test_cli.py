from __future__ import annotations

import json
import math
from argparse import Namespace
from datetime import UTC, date
from pathlib import Path

import pandas as pd
import pytest

from stock_grader import cli, pipeline
from stock_grader.profiles import ConsensusResult, profile_names
from stock_grader.types import GradeReport, SecuritySnapshot


def _report(ticker: str, score: float, letter: str, profile: str = "quality") -> GradeReport:
    return GradeReport(
        ticker=ticker,
        asof=date(2026, 7, 28),
        profile=profile,
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
    monkeypatch.setattr(
        cli,
        "_load_universe_selection",
        lambda path: cli._empty_selection(path, ["LOW", "HIGH", "REFUSED"]),
    )
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
    monkeypatch.setattr(
        cli, "_load_universe_selection", lambda path: cli._empty_selection(path, ["LOW", "HIGH"])
    )
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
    frame.attrs["split_events"] = [{"date": "2025-06-01", "factor": 4.0, "ratio": "4:1"}]

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


def test_universe_selection_parses_provenance_and_plain_loader_stays_compatible(
    tmp_path: Path,
) -> None:
    universe = tmp_path / "wide.txt"
    universe.write_text(
        "\n".join(
            [
                "# universe_id: liq1000_v1",
                "# asof: 2026-07-31",
                f"# spec_sha256: {'a' * 64}",
                f"# source_sha256: {'b' * 64}",
                "# row_count: 2",
                "brk-b",
                "aapl",
                "",
            ]
        ),
        encoding="utf-8",
    )

    selection = cli._load_universe_selection(str(universe))
    assert selection.tickers == ["BRK-B", "AAPL"]
    assert cli._load_universe(str(universe)) == selection.tickers
    assert selection.universe_id == "liq1000_v1"
    assert selection.asof == date(2026, 7, 31)
    assert selection.spec_sha256 == "a" * 64
    assert selection.source_sha256 == "b" * 64


def test_universe_selection_refuses_bad_count_future_use_and_staleness(tmp_path: Path) -> None:
    universe = tmp_path / "wide.txt"
    universe.write_text(
        "\n".join(
            [
                "# universe_id=liq1000_v1",
                "# asof=2026-07-31",
                f"# spec_sha256={'a' * 64}",
                f"# source_sha256={'b' * 64}",
                "# row_count=2",
                "AAPL",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="row_count"):
        cli._load_universe_selection(str(universe))

    selection = cli.UniverseSelection(
        ["AAPL"], "liq1000_v1", date(2026, 7, 31), "a" * 64, "b" * 64, str(universe)
    )
    with pytest.raises(SystemExit, match="earlier signal date"):
        cli._validate_universe_selection_asof(selection, date(2026, 7, 30))
    with pytest.raises(SystemExit, match="days old"):
        cli._validate_universe_selection_asof(selection, date(2027, 8, 1))


def test_rank_enforces_explicit_universe_selection_clock(tmp_path: Path) -> None:
    universe = tmp_path / "wide.txt"
    universe.write_text(
        "\n".join(
            [
                "# universe_id: liq1000_v1",
                "# asof: 2026-07-31",
                f"# spec_sha256: {'a' * 64}",
                f"# source_sha256: {'b' * 64}",
                "# row_count: 1",
                "AAPL",
            ]
        ),
        encoding="utf-8",
    )
    args = _common_args(
        universe=str(universe),
        asof="2027-08-01",
        top=None,
    )
    with pytest.raises(SystemExit, match="days old"):
        cli.cmd_rank(args)


def test_bulk_provider_policy_uses_shared_client_at_threshold(monkeypatch) -> None:
    clients: list[object] = []

    def client_factory(**_kwargs):
        client = object()
        clients.append(client)
        return client

    class Bulk:
        def __init__(self, client, *, cache_dir=None):
            self.client = client
            self.cache_dir = cache_dir
            self.ensure_calls: list[bool] = []

        def ensure(self, refresh: bool = False):
            self.ensure_calls.append(refresh)

    monkeypatch.setattr(cli, "SECClient", client_factory)
    monkeypatch.setattr(cli, "SECBulkFacts", Bulk)
    base = Namespace(
        cache_dir=None,
        contact=None,
        no_network=False,
        refresh=False,
        bulk_facts="auto",
    )
    assert cli._sec_provider_from_args(base, 199).bulk is None
    at_threshold = cli._sec_provider_from_args(base, 200)
    assert at_threshold.bulk is not None
    assert at_threshold.client is at_threshold.bulk.client
    assert at_threshold.bulk.ensure_calls == [False]
    offline = Namespace(**{**vars(base), "no_network": True})
    assert cli._sec_provider_from_args(offline, 1_000).bulk is None
    always = Namespace(**{**vars(offline), "bulk_facts": "always"})
    always_provider = cli._sec_provider_from_args(always, 1)
    assert always_provider.bulk is not None
    assert always_provider.bulk.ensure_calls == [False]
    assert len(clients) == 4


def test_new_grading_flags_are_registered_after_the_subcommand(tmp_path: Path) -> None:
    args = cli.build_parser().parse_args(
        [
            "freeze",
            "--universe",
            str(tmp_path / "u.txt"),
            "--bulk-facts",
            "always",
            "--vault",
            str(tmp_path / "Stock-Vault"),
        ]
    )
    assert args.bulk_facts == "always"
    assert args.vault.endswith("Stock-Vault")


def test_vault_price_provider_leads_the_cli_price_chain(monkeypatch, tmp_path: Path) -> None:
    created: list[tuple[object, Path | None]] = []

    class FakeProvider:
        name = "vault"

    def source_factory(path):
        return ("source", path)

    def provider_factory(source, *, cache_dir=None):
        created.append((source, cache_dir))
        return FakeProvider()

    monkeypatch.setattr(cli, "VaultDataSource", source_factory)
    monkeypatch.setattr(cli, "VaultPriceProvider", provider_factory)
    args = _common_args(
        vault=str(tmp_path / "Stock-Vault"),
        price_provider="auto",
        price_dir=str(tmp_path / "prices"),
        no_network=True,
        cache_dir=str(tmp_path / "cache"),
    )

    providers = cli._price_providers_from_args(args)
    assert providers[0].name == "vault"
    assert providers[1].name == "csv"
    assert created == [
        (("source", str(tmp_path / "Stock-Vault")), (tmp_path / "cache" / "vault").resolve())
    ]


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
        allow_mixed_universes=False,
        # Point at a scratch ledger: without this the run appends a junk trial
        # to the repo's real research_ledger.jsonl, deflating every future DSR.
        ledger=str(tmp_path / "ledger.jsonl"),
    )

    assert cli.cmd_backtest(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(payload["input_contract"].values())
    assert payload["periods"][0]["top_turnover"] == 1.0

    frame = pd.read_csv(path)
    frame["universe_id"] = ["narrow"] * 10 + ["wide"] * 10
    frame.to_csv(path, index=False)
    args.allow_mixed_universes = True
    assert cli.cmd_backtest(args) == 0
    mixed_payload = json.loads(capsys.readouterr().out)
    assert mixed_payload["input_contract"]["single_universe_id"] is False

    # The explicit waiver is narrow: a separate failed contract item still refuses the run.
    frame = frame.drop(columns=["universe_is_pit"])
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
            ledger=str(ledger),
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
            panel=str(panel),
            quantiles=2,
            min_cross_section=10,
            periods_per_year=12,
            transaction_cost_bps=10.0,
            bootstrap_samples=0,
            bootstrap_block_periods=1,
            seed=0,
            allow_unverified_panel=False,
            format="json",
            ledger=str(ledger),
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


def _prereg_panel_rows(months: tuple[int, ...]) -> list[dict]:
    return [
        {
            "signal_date": f"2025-{month:02d}-25",
            "filed_through": f"2025-{month:02d}-25",
            "return_start": f"2025-{month:02d}-26",
            "return_end": f"2025-{month + 1:02d}-25",
            "ticker": f"T{index}",
            "cik": f"{index + 1:010d}",
            "score": index + month / 10,
            "forward_return": (index + month) / 1_000,
            "profile": "all_weather",
            "config_fingerprint": "1790775d",
            "horizon_days": 21,
            "universe_is_pit": True,
            "return_is_total": True,
            "delisting_return_included": True,
        }
        for month in months
        for index in range(10)
    ]


def _prereg_backtest_args(panel: Path, ledger: Path) -> Namespace:
    return Namespace(
        panel=str(panel),
        quantiles=2,
        min_cross_section=10,
        periods_per_year=12,
        transaction_cost_bps=10.0,
        bootstrap_samples=0,
        bootstrap_block_periods=1,
        seed=0,
        allow_unverified_panel=False,
        format="json",
        ledger=str(ledger),
    )


def test_ledger_declare_is_append_once_and_idempotent(tmp_path, capsys) -> None:
    from stock_grader.research_manifest import (
        PREREGISTRATION_EXPERIMENT,
        load_manifest,
        verify_chain,
    )

    path = tmp_path / "panel.csv"
    pd.DataFrame(_prereg_panel_rows((1, 2, 3))).to_csv(path, index=False)
    ledger = tmp_path / "ledger.jsonl"
    declare = [
        "ledger-declare",
        str(path),
        "--quantiles",
        "2",
        "--min-cross-section",
        "10",
        "--ledger",
        str(ledger),
        "--schedule",
        "monthly (cron 41 2 6 * *)",
    ]

    assert cli.main(declare) == 0
    records = load_manifest(ledger)
    assert len(records) == 1
    assert records[0]["experiment"] == PREREGISTRATION_EXPERIMENT
    assert "monthly (cron 41 2 6 * *)" in records[0]["verdict"]
    assert verify_chain(records)

    # Declare-if-absent: the scheduled workflow re-declares every run.
    assert cli.main(declare) == 0
    assert len(load_manifest(ledger)) == 1, "an identical spec must not re-append"
    capsys.readouterr()


def test_preregistered_reevaluation_keeps_the_trial_denominator_flat(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The scheduled monthly look at ONE declared hypothesis is one trial.

    Without the declaration, two years of monthly re-evaluation deflated one
    hypothesis as ~24 trials and the near-duplicate Sharpes distorted
    stdev(trial_sharpes) — the E[max] benchmark in significance.py.
    """
    from stock_grader.research_manifest import load_manifest, trial_sharpes, verify_chain

    path = tmp_path / "panel.csv"
    pd.DataFrame(_prereg_panel_rows((1, 2, 3))).to_csv(path, index=False)
    ledger = tmp_path / "ledger.jsonl"
    assert (
        cli.main(
            [
                "ledger-declare",
                str(path),
                "--quantiles",
                "2",
                "--min-cross-section",
                "10",
                "--ledger",
                str(ledger),
                "--schedule",
                "monthly (cron 41 2 6 * *)",
            ]
        )
        == 0
    )
    capsys.readouterr()

    def run(panel: Path) -> dict:
        assert cli.cmd_backtest(_prereg_backtest_args(panel, ledger)) == 0
        return json.loads(capsys.readouterr().out)

    first = run(path)
    assert first["ledger"]["preregistered"] is True
    assert first["ledger"]["lifetime_trials"] == 1

    # Month two: the sample accrues, the spec is unchanged -> same ONE trial.
    pd.DataFrame(_prereg_panel_rows((1, 2, 3, 4))).to_csv(path, index=False)
    second = run(path)
    assert second["ledger"]["preregistered"] is True
    assert second["ledger"]["lifetime_trials"] == 1, "a scheduled look is not a new trial"

    records = load_manifest(ledger)
    assert verify_chain(records)
    assert records[-1]["trials"] == 1
    assert records[-1]["experiment"] == records[-2]["experiment"]
    assert records[-1]["experiment"].startswith("backtest:preregistered:all_weather:")
    assert records[-1]["symbols"] == [
        f"preregistration:{records[0]['integrity_sha256']}"
    ], "the result must reference the declaration it re-evaluates"
    assert records[-1]["verdict"].startswith("PRIMARY (pre-registered) -- ")
    assert "disclosed peeking, not corrected" in records[-1]["leakage_controls"]
    # Shared-denominator consistency: the collapse rule counts both
    # re-evaluations as ONE hypothesis for every later deflation (decay.py's
    # record_sweep_trials reads this same function).
    assert len(trial_sharpes(records)) == 1

    # An UNDECLARED spec (different evaluation parameters) is a new trial and
    # keeps today's behavior: the denominator grows.
    undeclared = _prereg_backtest_args(path, ledger)
    undeclared.transaction_cost_bps = 25.0
    assert cli.cmd_backtest(undeclared) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["ledger"]["preregistered"] is False
    assert third["ledger"]["lifetime_trials"] == 2

    # A tampered declaration (claimed hash != stored spec) must refuse to bless:
    # covered structurally in test_research_manifest; here the chain over the
    # mixed record kinds must still verify end-to-end.
    assert verify_chain(load_manifest(ledger))


def _freeze_args(out: Path, **overrides) -> Namespace:
    values = {
        "out": str(out),
        "universe": "ignored.txt",
        "asof": "2026-07-29",
        "pit": False,
        "refresh": False,
        "all_profiles": False,
    }
    values.update(overrides)
    return _common_args(**values)


def _patch_freeze_universe(monkeypatch, snapshots, **metadata) -> None:
    selection = cli.UniverseSelection(
        tickers=[snapshot.ticker for snapshot in snapshots],
        universe_id=metadata.get("universe_id"),
        asof=metadata.get("asof"),
        spec_sha256=metadata.get("spec_sha256"),
        source_sha256=metadata.get("source_sha256"),
        path="ignored.txt",
    )
    monkeypatch.setattr(cli, "_load_universe_selection", lambda _path: selection)


def test_freeze_writes_immutable_dated_panel(tmp_path, monkeypatch):
    """The forward panel: one parquet per profile per date, never overwritten."""
    from tests.test_pipeline import _universe

    snapshots = _universe(16, with_prices=False)
    _patch_freeze_universe(
        monkeypatch,
        snapshots,
        universe_id="liq1000_v1",
        asof=date(2026, 7, 29),
        spec_sha256="a" * 64,
        source_sha256="b" * 64,
    )
    monkeypatch.setattr(cli, "_build_snapshots", lambda tickers, args, provider: snapshots)

    args = _freeze_args(tmp_path / "frozen", profile="all_weather")
    assert cli.cmd_freeze(args) == 0
    # Single-profile mode writes into the profile's own subdirectory too, so the
    # layout is identical whichever mode produced a panel.
    out = tmp_path / "frozen" / "all_weather" / "2026-07-29.parquet"
    assert out.exists()
    frame = pd.read_parquet(out)
    assert len(frame) == 16
    assert set(frame.columns) >= {
        "signal_date",
        "ticker",
        "cik",
        "score",
        "letter",
        "graded",
        "config_fingerprint",
        "universe_fingerprint",
        "code_commit",
    }
    before = out.read_bytes()
    assert set(frame["universe_id"]) == {"liq1000_v1"}
    assert set(frame["universe_spec_sha256"]) == {"a" * 64}
    assert cli.cmd_freeze(args) == 0  # second run: skip, never overwrite
    assert out.read_bytes() == before


def test_freeze_with_foundry_attaches_dps_fallback_to_snapshots(tmp_path, monkeypatch):
    """The scheduled freeze is the only run that produces forward evidence, so
    it must actually exercise the foundry dividend fallback: a freeze given
    --foundry attaches foundry_dps_ttm to the snapshots it grades."""
    from tests.test_foundry import build_foundry

    foundry_root = build_foundry(tmp_path / "foundry")
    tickers = ["AAPL"] + [f"T{index:02d}" for index in range(15)]
    _patch_freeze_universe(
        monkeypatch,
        [SecuritySnapshot(ticker=ticker, asof=date(2026, 7, 29)) for ticker in tickers],
    )

    class _Provider:
        def fetch(self, ticker, *, asof, pit_mode, refresh):
            return SecuritySnapshot(ticker=ticker, asof=asof)

    monkeypatch.setattr(cli, "_sec_provider_from_args", lambda _args, _count: _Provider())

    captured: dict[str, list[SecuritySnapshot]] = {}

    def fake_grade(snapshots, _config):
        captured["snapshots"] = snapshots
        return {
            snapshot.ticker: _report(snapshot.ticker, 50.0, "B", profile="all_weather")
            for snapshot in snapshots
        }

    monkeypatch.setattr(cli, "grade_universe", fake_grade)

    args = cli.build_parser().parse_args(
        [
            "freeze",
            "--universe",
            "ignored.txt",
            "--out",
            str(tmp_path / "frozen"),
            "--asof",
            "2026-07-29",
            "--no-network",
            "--no-sec-prices",
            "--foundry",
            str(foundry_root),
        ]
    )
    assert cli.cmd_freeze(args) == 0
    assert (tmp_path / "frozen" / "all_weather" / "2026-07-29.parquet").exists()

    by_ticker = {snapshot.ticker: snapshot for snapshot in captured["snapshots"]}
    assert len(by_ticker) == 16
    # Every graded snapshot records that a verified foundry was consulted…
    assert all(s.meta.get("foundry_status") == "verified" for s in by_ticker.values())
    # …the fixture's dividend payer carries the trailing sum the fallback needs…
    assert by_ticker["AAPL"].meta["foundry_dps_ttm"] == pytest.approx(0.50)
    assert by_ticker["AAPL"].meta["foundry_dps_source"] == "stock-data corporate_actions"
    # …and a ticker the foundry has no rows for stays unknown, never zero.
    assert "foundry_dps_ttm" not in by_ticker["T00"].meta


def test_freeze_refuses_a_nearly_ungraded_panel(
    tmp_path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An EDGAR outage that fails every gate must not freeze a tradeable-looking parquet."""
    from tests.test_pipeline import _universe

    snapshots = _universe(16, with_prices=False)
    _patch_freeze_universe(monkeypatch, snapshots)
    monkeypatch.setattr(cli, "_build_snapshots", lambda tickers, args, provider: snapshots)
    # Every gate refused a letter: scores exist but nothing is graded.
    monkeypatch.setattr(
        cli,
        "grade_universe",
        lambda _snapshots, _config: {s.ticker: _report(s.ticker, 50.0, "N/A") for s in snapshots},
    )

    args = _freeze_args(tmp_path / "frozen", profile="all_weather")
    assert cli.cmd_freeze(args) == 2
    assert not (tmp_path / "frozen" / "all_weather" / "2026-07-29.parquet").exists()
    assert "refusing to freeze" in capsys.readouterr().out


def test_freeze_all_profiles_writes_one_panel_per_registered_profile(tmp_path, monkeypatch):
    """One SEC fetch, one snapshot build, a panel per style lens."""
    from tests.test_pipeline import _universe

    snapshots = _universe(16, with_prices=False)
    builds = []

    def build_snapshots(tickers, args, provider):
        builds.append(tickers)
        return snapshots

    _patch_freeze_universe(monkeypatch, snapshots)
    monkeypatch.setattr(cli, "_build_snapshots", build_snapshots)

    def grade_all(snaps, configs):
        return {
            config.name: {
                snapshot.ticker: _report(snapshot.ticker, 50.0, "B", profile=config.name)
                for snapshot in snaps
            }
            for config in configs
        }

    monkeypatch.setattr(pipeline, "grade_universe_multi", grade_all)

    args = _freeze_args(tmp_path / "frozen", all_profiles=True)
    assert cli.cmd_freeze(args) == 0
    assert len(builds) == 1  # the extra profiles must cost no extra fetching
    for profile in profile_names():
        panel = tmp_path / "frozen" / profile / "2026-07-29.parquet"
        assert panel.exists(), profile
        frame = pd.read_parquet(panel)
        assert len(frame) == 16
        assert set(frame["profile"]) == {profile}

    before = {p: p.read_bytes() for p in (tmp_path / "frozen").rglob("*.parquet")}
    assert cli.cmd_freeze(args) == 0  # immutability holds for every profile
    assert {p: p.read_bytes() for p in (tmp_path / "frozen").rglob("*.parquet")} == before
    assert len(builds) == 1  # nothing left to freeze: no fetch at all


def _refusing_universe(monkeypatch, refuse: set[str]):
    """Grade every profile in `refuse` as ungraded, the rest as graded B."""
    from tests.test_pipeline import _universe

    snapshots = _universe(16, with_prices=False)
    _patch_freeze_universe(monkeypatch, snapshots)
    monkeypatch.setattr(cli, "_build_snapshots", lambda tickers, args, provider: snapshots)

    def grade_all(snaps, configs):
        return {
            config.name: {
                snapshot.ticker: _report(
                    snapshot.ticker,
                    50.0,
                    "N/A" if config.name in refuse else "B",
                    profile=config.name,
                )
                for snapshot in snaps
            }
            for config in configs
        }

    monkeypatch.setattr(pipeline, "grade_universe_multi", grade_all)


def test_freeze_all_profiles_skips_one_refused_profile_without_failing(
    tmp_path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The interlock is per profile, and a profile that never graded is not news.

    momentum and low_volatility cannot grade without a dense price series, so
    failing the monthly run for that known state would train the operator to
    ignore the alarm entirely.
    """
    _refusing_universe(monkeypatch, {"momentum"})

    args = _freeze_args(tmp_path / "frozen", all_profiles=True)
    assert cli.cmd_freeze(args) == 0  # structural refusal: the run stays green
    assert not (tmp_path / "frozen" / "momentum" / "2026-07-29.parquet").exists()
    for profile in profile_names():
        if profile != "momentum":
            assert (tmp_path / "frozen" / profile / "2026-07-29.parquet").exists(), profile
    out = capsys.readouterr().out
    assert "refusing to freeze" in out
    assert "momentum" in out  # the refusal names the profile that was skipped
    assert "structural" in out


def test_freeze_structural_refusals_are_idempotent_when_sibling_panels_exist(
    tmp_path,
    monkeypatch,
) -> None:
    """A rerun may find only known refusals pending after siblings were frozen."""
    refused = {"momentum", "low_volatility"}
    _refusing_universe(monkeypatch, refused)
    args = _freeze_args(tmp_path / "frozen", all_profiles=True)

    assert cli.cmd_freeze(args) == 0
    existing = {
        path: path.read_bytes() for path in (tmp_path / "frozen").rglob("*.parquet")
    }
    assert set(profile_names()) - refused == {path.parent.name for path in existing}

    # On the old alarm, both remaining pending profiles refusing made this exit
    # 2 even though all profiles capable of grading already had same-date panels.
    assert cli.cmd_freeze(args) == 0
    assert {
        path: path.read_bytes() for path in (tmp_path / "frozen").rglob("*.parquet")
    } == existing


def test_freeze_fails_when_a_profile_that_froze_before_refuses_now(
    tmp_path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile with history refusing IS news: something broke, so go red."""
    _refusing_universe(monkeypatch, {"momentum"})
    prior = tmp_path / "frozen" / "momentum" / "2026-06-30.parquet"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"prior panel")

    args = _freeze_args(tmp_path / "frozen", all_profiles=True)
    assert cli.cmd_freeze(args) == 2
    assert "regression" in capsys.readouterr().out


def test_freeze_fails_when_the_traded_profile_refuses(tmp_path, monkeypatch) -> None:
    """all_weather is what the paper trader trades; its refusal breaks trading."""
    _refusing_universe(monkeypatch, {cli.TRADED_PROFILE})

    args = _freeze_args(tmp_path / "frozen", all_profiles=True)
    assert cli.cmd_freeze(args) == 2


def test_freeze_fails_when_every_profile_refuses(tmp_path, monkeypatch) -> None:
    """Nothing written at all is a data outage, whatever the per-profile story."""
    _refusing_universe(monkeypatch, set(profile_names()))

    args = _freeze_args(tmp_path / "frozen", all_profiles=True)
    assert cli.cmd_freeze(args) == 2


def test_freeze_all_profiles_is_registered_on_the_subcommand(tmp_path) -> None:
    """Workflows pass it subcommand-first; a top-level-only flag would exit 2 in cron."""
    args = cli.build_parser().parse_args(
        ["freeze", "--universe", str(tmp_path / "u.txt"), "--all-profiles"]
    )
    assert args.all_profiles is True
    assert cli.build_parser().parse_args(["freeze", "--universe", "u.txt"]).all_profiles is False


def test_shipped_panels_live_under_their_profile_directory() -> None:
    """Layout v2 (pinned cross-repo): frozen_scores/<profile>/<date>.parquet, no flat files."""
    frozen = Path(__file__).resolve().parent.parent / "frozen_scores"
    assert (frozen / "all_weather" / "2026-07-30.parquet").exists()
    assert not list(frozen.glob("*.parquet"))  # the legacy flat panel was migrated, not copied


def test_monthly_freeze_workflow_fails_fast_without_sec_contact() -> None:
    """An unset SEC_CONTACT_EMAIL must abort the freeze, not degrade the SEC User-Agent."""
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monthly-freeze.yml"
    ).read_text(encoding="utf-8")

    guard = workflow.find('if [ -z "$STOCK_GRADER_CONTACT" ]')
    invocation = workflow.find("stock-grader freeze")
    assert guard != -1, "freeze step must fail fast when STOCK_GRADER_CONTACT is empty"
    assert guard < invocation
    guard_block = workflow[guard:invocation]
    assert "SEC_CONTACT_EMAIL" in guard_block  # the message names the secret to set
    assert "exit 1" in guard_block


def test_monthly_freeze_workflow_freezes_every_profile() -> None:
    """The monthly clock is the only source of forward evidence — it must cover all profiles."""
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monthly-freeze.yml"
    ).read_text(encoding="utf-8")

    invocation = next(line for line in workflow.splitlines() if "stock-grader freeze" in line)
    assert "--all-profiles" in invocation
    assert "--profile " not in invocation  # not pinned to one lens any more


def test_monthly_freeze_workflow_keeps_deep_clock_and_adds_wide_bulk_clock() -> None:
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monthly-freeze.yml"
    ).read_text(encoding="utf-8")

    assert "timeout-minutes: 300" in workflow
    assert "df -h" in workflow
    invocations = [line.strip() for line in workflow.splitlines() if "stock-grader freeze" in line]
    assert invocations[0] == (
        "stock-grader freeze --all-profiles --universe config/universe_default.txt "
        '--out frozen_scores "${FOUNDRY_ARGS[@]}"'
    )
    assert len(invocations) == 2
    # The wide universe is resolved through a pointer so the quarterly
    # regeneration can advance it without editing the workflow; the pointer must
    # name a dated artifact that actually exists.
    assert 'WIDE="config/$(cat config/universe_wide_current)"' in workflow
    assert '--universe "$WIDE"' in workflow
    pointer = (
        Path(__file__).resolve().parent.parent / "config" / "universe_wide_current"
    ).read_text(encoding="utf-8").strip()
    assert (Path(__file__).resolve().parent.parent / "config" / pointer).is_file(), (
        f"universe pointer names a missing file: {pointer}"
    )
    assert "--out frozen_scores_wide" in workflow
    assert "--bulk-facts auto" in workflow
    assert "--price-provider sec" in workflow
    assert "git add -A frozen_scores frozen_scores_wide" in workflow


def test_monthly_freeze_workflow_wires_the_foundry_dividend_fallback() -> None:
    """The scheduled freeze produces all forward evidence, so it must hand the
    foundry to BOTH freeze invocations — and only when its checkout succeeded,
    because --foundry fails closed and a checkout flake must cost the fallback,
    never the month's panels."""
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monthly-freeze.yml"
    ).read_text(encoding="utf-8")

    # A local checkout, not url_base mode: reads are hash-verified files that
    # cannot flake mid-run, and the directory stays out of the committed paths.
    assert "repository: TylerJForstrom/Stock-Data" in workflow
    assert "path: stock-data" in workflow
    assert "id: stock_data" in workflow
    checkout = workflow.split("- name: Check out Stock-Data foundry", 1)[1]
    checkout = checkout.split("      - ", 1)[0]
    assert any(
        line.strip() == "continue-on-error: true"
        for line in checkout.splitlines()
        if not line.lstrip().startswith("#")
    ), "a Stock-Data checkout flake must not abort the job before any panel freezes"

    # Both freeze invocations receive the guarded foundry argument.
    freeze_lines = [line for line in workflow.splitlines() if "stock-grader freeze" in line]
    assert len(freeze_lines) == 2
    assert '"${FOUNDRY_ARGS[@]}"' in freeze_lines[0]
    wide_step = workflow.split("- name: Freeze wide", 1)[1].split("- name: Commit", 1)[0]
    assert '"${FOUNDRY_ARGS[@]}"' in wide_step
    assert workflow.count("FOUNDRY_ARGS=(--foundry stock-data)") == 2
    assert workflow.count("steps.stock_data.outcome == 'success'") == 2

    # The missing-fallback alarm fires AFTER the commit: panels frozen without
    # the fallback are still point-in-time evidence and must reach the branch.
    alarm = workflow.find("- name: Alarm if the foundry fallback was unavailable")
    commit = workflow.find("- name: Commit")
    assert alarm != -1 and commit != -1 and commit < alarm
    alarm_step = workflow[alarm:]
    assert "if: always() && steps.stock_data.outcome != 'success'" in alarm_step
    assert "exit 1" in alarm_step


def test_monthly_freeze_workflow_retries_the_triggering_branch() -> None:
    """A concurrent branch push must not make the retry rebase onto main."""
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monthly-freeze.yml"
    ).read_text(encoding="utf-8")
    commit_step = workflow.split("- name: Commit", 1)[1]

    assert "fetch-depth: 0" in workflow
    branch_guard = 'if [ "$GITHUB_REF_TYPE" != "branch" ]'
    target = 'target_branch="$GITHUB_REF_NAME"'
    fetch = 'git fetch origin "+refs/heads/$target_branch:$remote_ref"'
    rebase = 'git rebase "$remote_ref"'
    push = 'git push origin "HEAD:refs/heads/$target_branch"'
    for required in (branch_guard, target, fetch, rebase, push):
        assert required in commit_step
    assert commit_step.index(fetch) < commit_step.index(rebase) < commit_step.index(push)
    assert "git pull --rebase origin main" not in commit_step
    assert "if git push;" not in commit_step


def test_monthly_freeze_commits_panels_even_when_a_profile_refuses() -> None:
    """Regression: a refusal must not discard the panels that DID freeze.

    Both freeze steps write their valid parquet panels before cmd_freeze's alarm
    policy runs, and that policy exits 2 whenever a profile with a prior panel
    refuses — which every one of the nine wide profiles now has. Without
    `if: always()` on the commit step, one profile refusing throws away every good
    panel from that run, and a frozen panel is point-in-time evidence that cannot
    be backfilled on the next tick. The run still goes red; it just stops taking
    the month's evidence down with it.
    """
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monthly-freeze.yml"
    ).read_text(encoding="utf-8")

    def step_block(name: str) -> list[str]:
        """The lines of exactly one step: its `- name:` line to the next list item."""
        chunk = workflow.split(f"      - name: {name}", 1)[1]
        lines: list[str] = []
        for line in chunk.splitlines():
            if line.startswith("      - "):  # the next step begins
                break
            lines.append(line)
        return lines

    def has_always_guard(block: list[str]) -> bool:
        # Match the step KEY, not the prose. The step is commented, and those
        # comments quote `if: always()` — a substring search over the raw text is
        # satisfied by the explanation of the guard rather than the guard itself.
        return any(
            line.strip() == "if: always()"
            for line in block
            if not line.lstrip().startswith("#")
        )

    assert has_always_guard(step_block("Commit")), (
        "the Commit step must run even after a freeze exits 2, or a single profile "
        "refusal discards every valid panel from that run"
    )
    assert has_always_guard(step_block("Freeze wide")), (
        "the wide freeze must still run when the narrow freeze refused"
    )


def test_build_panel_json_format_reports_readiness(tmp_path, capsys):
    """The workflow reads `ready_for_backtest` from stdout; nothing matured -> 0."""
    from tests.test_panel import SIGNALS, _build_market_vault, _healthy_rows, _write_frozen

    vault_root = _build_market_vault(tmp_path / "vault")
    frozen = tmp_path / "frozen"
    # Signal on the archive's last day: entry never arrives, nothing matures.
    _write_frozen(frozen, date(2026, 9, 10), _healthy_rows())
    del SIGNALS  # imported for parity with test_panel fixtures; unused here

    exit_code = cli.main(
        [
            "build-panel",
            "--profile",
            "all_weather",
            "--frozen-root",
            str(frozen),
            "--vault",
            str(vault_root),
            "--out",
            str(tmp_path / "out"),
            "--horizon-days",
            "5",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_for_backtest"] is False
    assert payload["matured_signal_dates"] == []
    assert not (tmp_path / "out" / "all_weather.parquet").exists()
    assert (tmp_path / "out" / "all_weather.build.json").exists()


def test_ledger_retract_appends_and_keeps_chain_valid(tmp_path):
    from stock_grader.research_manifest import (
        ResearchRecord,
        append_record,
        load_manifest,
        verify_chain,
    )

    ledger = tmp_path / "ledger.jsonl"
    append_record(
        ledger,
        ResearchRecord(
            experiment="backtest:junk.csv",
            market="us_equities",
            symbols=[],
            targets=[],
            horizons=[1],
            trials=1,
            metrics={"per_period_sharpe": 2.83},
            costs={},
            benchmark="none",
            leakage_controls="PASS",
            gate_passed=False,
            verdict="NO EDGE",
        ),
    )
    junk_hash = load_manifest(ledger)[0]["integrity_sha256"]

    assert (
        cli.main(
            ["ledger-retract", junk_hash, "--ledger", str(ledger), "--reason", "synthetic fixture"]
        )
        == 0
    )
    records = load_manifest(ledger)
    assert len(records) == 2
    assert records[-1]["experiment"] == "ledger:retraction"
    assert records[-1]["symbols"] == [junk_hash]
    assert verify_chain(records)

    # Refusals: unknown hash, and retracting a retraction.
    assert cli.main(["ledger-retract", "f" * 64, "--ledger", str(ledger), "--reason", "x"]) == 2
    retraction_hash = records[-1]["integrity_sha256"]
    assert (
        cli.main(["ledger-retract", retraction_hash, "--ledger", str(ledger), "--reason", "x"])
        == 2
    )


# -- run journal + diff --since-last -------------------------------------------


def _journaled_report(
    ticker: str,
    score: float,
    letter: str,
    config_fp: str,
    *,
    contributions: dict[str, float] | None = None,
    asof: date = date(2026, 7, 28),
) -> GradeReport:
    return GradeReport(
        ticker=ticker,
        asof=asof,
        profile="all_weather",
        score=score,
        letter=letter,
        coverage=0.9,
        explain={"metric_contributions": contributions or {}},
        meta={
            "cik": None,
            "sector": "general",
            "config_fingerprint": config_fp,
            "universe_fingerprint": "u" * 64,
        },
    )


def test_cmd_grade_journals_runs_and_seeds_hysteresis(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """First grade run appends a journal record; the second reads its letters back."""
    _patch_data_loading(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_peers", lambda _args, _tickers: [])
    seen_previous: list[dict[str, str] | None] = []
    letters = iter(["B+", "B", "B"])

    def fake_grade_universe(snapshots, config, *, previous_letters=None):
        seen_previous.append(previous_letters)
        fingerprint = cli.config_fingerprint(config)
        return {"AAPL": _journaled_report("AAPL", 71.2, next(letters), fingerprint)}

    monkeypatch.setattr(cli, "grade_universe", fake_grade_universe)
    args = _common_args(
        tickers=["AAPL"],
        explain=False,
        journal_dir=str(tmp_path),
        no_journal=False,
        hysteresis=True,
        format="json",
    )

    assert cli.cmd_grade(args) == 0
    assert seen_previous == [None], "a first run has no comparable baseline"
    assert len(list(tmp_path.glob("*.json"))) == 1

    assert cli.cmd_grade(args) == 0
    assert seen_previous[1] == {"AAPL": "B+"}, "second run must seed hysteresis"
    assert len(list(tmp_path.glob("*.json"))) == 2

    # Without --hysteresis the journal still appends, but prior letters stay
    # out of the grade: SPEC design decision D9 keeps prior state opt-in.
    args.hysteresis = False
    assert cli.cmd_grade(args) == 0
    assert seen_previous[2] is None
    assert len(list(tmp_path.glob("*.json"))) == 3
    capsys.readouterr()


def test_cmd_grade_no_journal_disables_reading_and_writing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _patch_data_loading(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_peers", lambda _args, _tickers: [])
    seen_previous: list[dict[str, str] | None] = []

    def fake_grade_universe(snapshots, config, *, previous_letters=None):
        seen_previous.append(previous_letters)
        return {"AAPL": _journaled_report("AAPL", 71.2, "B+", cli.config_fingerprint(config))}

    monkeypatch.setattr(cli, "grade_universe", fake_grade_universe)
    args = _common_args(
        tickers=["AAPL"],
        explain=False,
        journal_dir=str(tmp_path),
        no_journal=True,
        format="json",
    )
    assert cli.cmd_grade(args) == 0
    assert seen_previous == [None]
    assert not list(tmp_path.glob("*.json"))
    capsys.readouterr()


def test_diff_since_last_reports_deltas_and_movers(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from datetime import datetime

    from stock_grader import journal

    config_fp = "c" * 64
    journal.append_run(
        {
            "AAPL": _journaled_report(
                "AAPL", 71.2, "B+", config_fp, contributions={"roe": 2.0, "margin": 1.0}
            )
        },
        journal_dir=tmp_path,
        recorded_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    journal.append_run(
        {
            "AAPL": _journaled_report(
                "AAPL",
                68.9,
                "B",
                config_fp,
                contributions={"roe": -1.0, "margin": 1.2},
                asof=date(2026, 8, 3),
            )
        },
        journal_dir=tmp_path,
        recorded_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert (
        cli.main(
            ["diff", "AAPL", "--since-last", "--journal-dir", str(tmp_path), "--format", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["letter"] == {"from": "B+", "to": "B", "changed": True}
    assert payload["score"]["delta"] == pytest.approx(-2.3)
    assert payload["fingerprint_drift"] == []
    assert [entry["metric"] for entry in payload["metric_movers"]] == ["roe", "margin"]
    assert "not investment" in payload["disclaimer"]

    # The text rendering carries the same story plus the mandatory disclaimer.
    assert cli.main(["diff", "AAPL", "--since-last", "--journal-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "B+ → B" in text
    assert "roe" in text
    assert "not investment" in text


def test_diff_refuses_mismatched_fingerprints_unless_overridden(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from datetime import datetime

    from stock_grader import journal

    journal.append_run(
        {"AAPL": _journaled_report("AAPL", 71.2, "B+", "c" * 64)},
        journal_dir=tmp_path,
        recorded_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    journal.append_run(
        {"AAPL": _journaled_report("AAPL", 68.9, "B", "d" * 64)},
        journal_dir=tmp_path,
        recorded_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert cli.main(["diff", "AAPL", "--since-last", "--journal-dir", str(tmp_path)]) == 2
    assert "comparable only when fingerprints match" in capsys.readouterr().out

    assert (
        cli.main(
            [
                "diff",
                "AAPL",
                "--since-last",
                "--journal-dir",
                str(tmp_path),
                "--allow-fingerprint-drift",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["fingerprint_drift"], "an accepted regime break must stay visible"


def test_diff_refuses_without_a_baseline(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert cli.main(["diff", "AAPL", "--since-last", "--journal-dir", str(tmp_path)]) == 2
    assert "no journaled run contains AAPL" in capsys.readouterr().out


def test_diff_requires_since_last() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["diff", "AAPL"])


def test_promotion_declare_policy_then_transition_flow(tmp_path, capsys) -> None:
    """The full public half of the promotion split, end to end via the CLI."""
    from stock_grader.research_manifest import load_manifest, trial_sharpes, verify_chain

    doc = tmp_path / "PROMOTION.md"
    doc.write_text("PROMOTION-POLICY v1 bytes\n", encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    policy = [
        "promotion-declare",
        "--ledger",
        str(ledger),
        "--policy-doc",
        str(doc),
        "--policy-version",
        "promotion-policy-v1",
    ]

    assert cli.main(policy) == 0
    # Declare-if-absent: identical version+hash appends nothing.
    assert cli.main(policy) == 0
    assert len(load_manifest(ledger)) == 1

    # A changed document under the SAME version is refused: amendment is a
    # NEW version, superseded declarations stay.
    doc.write_text("quietly edited policy\n", encoding="utf-8")
    assert cli.main(policy) == 2
    # rich wraps console output at terminal width (CI widths differ per OS),
    # so strip the wrap newlines before any substring assertion.
    assert "NEW version" in capsys.readouterr().out.replace("\n", "")
    doc.write_text("PROMOTION-POLICY v1 bytes\n", encoding="utf-8")

    subject = "ab" * 32
    transition = [
        *policy,
        "--subject",
        subject,
        "--subject-label",
        "borrow_fee_level",
        "--from-stage",
        "exploratory",
        "--to-stage",
        "declared_trial",
        "--evidence",
        "cd" * 32,
        "--evidence-journal",
        "Stock-Vault data/decision_journal/decisions.jsonl.gz",
        "--evidence-journal-head",
        "ef" * 32,
        "--reason",
        "spec + schedule declared in the vault decision journal",
    ]
    assert cli.main(transition) == 0
    capsys.readouterr()

    records = load_manifest(ledger)
    assert verify_chain(records)
    assert len(records) == 2
    line = records[-1]
    assert line["experiment"] == "ledger:promotion"
    assert line["verdict"].startswith("PROMOTION: borrow_fee_level")
    # Licensing wall: the public record carries hashes and names only, and no
    # metrics — the trial denominator must be untouched.
    assert line["metrics"] == {}
    assert trial_sharpes(records) == []

    # Replaying the exact transition is refused: the subject already moved.
    assert cli.main(transition) == 2
    assert "does not match" in capsys.readouterr().out.replace("\n", "")

    # Climbing more than one rung from the recorded stage is refused (and the
    # live-money rung itself is pinned unreachable in the unit tests).
    live = list(transition)
    live[live.index("exploratory")] = "declared_trial"
    live[live.index("declared_trial", live.index("--to-stage"))] = "live_money"
    assert cli.main(live) == 2
    assert "exactly one rung" in capsys.readouterr().out.replace("\n", "")


def test_promotion_declare_refuses_broken_chain_and_bad_inputs(tmp_path, capsys) -> None:
    from stock_grader.research_manifest import (
        promotion_policy_declaration,
        promotion_policy_record,
    )

    doc = tmp_path / "PROMOTION.md"
    doc.write_text("policy bytes\n", encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    base = [
        "promotion-declare",
        "--ledger",
        str(ledger),
        "--policy-doc",
        str(doc),
        "--policy-version",
        "promotion-policy-v1",
    ]

    # Missing policy document.
    missing = [*base]
    missing[missing.index(str(doc))] = str(tmp_path / "absent.md")
    assert cli.main(missing) == 2
    capsys.readouterr()

    # A partial transition flag set is refused before touching the ledger.
    assert cli.main([*base, "--subject", "ab" * 32]) == 2
    assert "together" in capsys.readouterr().out.replace("\n", "")

    # Broken chain: two records whose linkage was severed by deleting the
    # middle line. The CLI must refuse to append anything.
    import hashlib as _hashlib

    declaration = promotion_policy_declaration(
        policy_version="promotion-policy-v0",
        policy_doc=str(doc),
        policy_sha256=_hashlib.sha256(doc.read_bytes()).hexdigest(),
    )
    from stock_grader.research_manifest import append_record

    append_record(ledger, promotion_policy_record(declaration, code_commit="test"))
    append_record(ledger, promotion_policy_record(declaration, code_commit="test2"))
    append_record(ledger, promotion_policy_record(declaration, code_commit="test3"))
    lines = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    assert cli.main(base) == 2
    assert "refusing to append to a broken chain" in capsys.readouterr().out.replace("\n", "")
