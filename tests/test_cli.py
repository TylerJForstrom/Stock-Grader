from __future__ import annotations

import json
import math
from argparse import Namespace
from datetime import date
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
        "--out frozen_scores"
    )
    assert len(invocations) == 2
    assert "--universe config/universe_liq1000_2026-07-30.txt" in workflow
    assert "--out frozen_scores_wide" in workflow
    assert "--bulk-facts auto" in workflow
    assert "--price-provider sec" in workflow
    assert "git add -A frozen_scores frozen_scores_wide" in workflow


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
