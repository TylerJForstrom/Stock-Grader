"""Regression tests for the 2026-08-04 confirmed-defect audit.

One module, because the defects share a single theme: something optimistic
happened silently where the ecosystem's rules require a computed number or an
honest refusal. Each test fails on the code as it stood at f90986b.

Grouped by the artifact whose honesty was at stake:

1. the evaluable signal panel (rollup vs accounting, coverage, provenance),
2. the split guard (the foundry table as a detector, not a confirmer),
3. the research ledger (hashes, retraction scope, policy binding, chain gates),
4. the frozen-panel catalog (immutability, attestation),
5. the cadence clocks and the monthly workflow.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest
from tests.test_signal_panel import (
    DAYS,
    SIGNAL,
    _build_vault_root,
    _gz_jsonl,
    _hazard_rows,
    _observation,
    _write_observations,
)

from stock_grader import cadence, cli
from stock_grader.data.vault import VaultDataSource
from stock_grader.panel import split_factor
from stock_grader.signal_panel import (
    SignalPanelConfig,
    SignalPanelError,
    build_signal_panel,
    write_signal_panel,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FORWARD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "monthly-forward-backtest.yml"


def _flat(text: str) -> str:
    """Console output with ALL whitespace removed.

    `rich` hard-wraps to the terminal width, and the wrap point moves with the
    tmp_path length — so a message can arrive as "does\nnot verify". Dropping
    newlines alone then glues words together. Compare whitespace-free.
    """
    return "".join(text.split())


LATER_SIGNAL = dt.date(2026, 8, 10)
LATER_ENTRY = dt.date(2026, 8, 11)
LATER_EXIT = dt.date(2026, 8, 14)


@pytest.fixture()
def vault_root(tmp_path: Path) -> Path:
    return _build_vault_root(tmp_path / "vault")


def _build(root: Path, **config_kwargs):
    vault = VaultDataSource(root)
    result, parts = build_signal_panel(
        vault, "unit_signal", config=SignalPanelConfig(**config_kwargs)
    )
    if result.refusal is not None:
        return result, None
    return result, write_signal_panel(vault, "unit_signal", result, parts)


# =========================================================== 1. signal panel


def test_a_part_the_accounting_does_not_cover_is_refused(vault_root: Path) -> None:
    """panel.parquet came from a glob, every number came from counts.json.

    Nothing reconciled them, so a part on disk with no counts entry contributed
    its rows to the rollup while contributing nothing to the numbers — and all
    three attestations stamped onto those rows were computed without them.
    """
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    _, panel_path = _build(vault_root)
    directory = panel_path.parent

    counts_path = directory / "counts.json"
    counts_path.write_text("{}\n", encoding="utf-8")  # part survives, accounting gone

    vault = VaultDataSource(vault_root)
    empty, parts = build_signal_panel(vault, "unit_signal", config=SignalPanelConfig())
    with pytest.raises(SignalPanelError, match="no entry in counts.json"):
        write_signal_panel(vault, "unit_signal", empty, parts)


def test_a_rollup_whose_rows_outnumber_its_accounting_is_refused(vault_root: Path) -> None:
    """The divergence the stale-part scenario actually produces.

    Overwriting a date's accounting with a smaller kept count leaves the part on
    disk contributing rows the attestations never saw. Equal key sets are not
    enough; the row COUNTS have to agree.
    """
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    _, panel_path = _build(vault_root)
    counts_path = panel_path.parent / "counts.json"
    counts = json.loads(counts_path.read_text())
    counts[SIGNAL.isoformat()]["kept"] = 0
    counts[SIGNAL.isoformat()]["unresolved_dropped"] = 0
    counts_path.write_text(json.dumps(counts), encoding="utf-8")

    vault = VaultDataSource(vault_root)
    stale, parts = build_signal_panel(vault, "unit_signal", config=SignalPanelConfig())
    with pytest.raises(SignalPanelError, match="rollup and counts.json disagree"):
        write_signal_panel(vault, "unit_signal", stale, parts)


def test_a_rebuild_that_prices_a_closed_date_to_zero_rows_is_refused(
    vault_root: Path,
) -> None:
    """The archive rolled past the window: kept=0, no new part, stale part kept.

    Before: counts[date] was rewritten to kept=0/unresolved=0 while the previous
    build's part kept feeding panel.parquet, flipping universe_is_pit and
    delisting_return_included to True over rows whose outcome-dependent drops
    had just been erased from the ledger.
    """
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    first, panel_path = _build(vault_root)
    assert first.kept_rows > 0

    # Keep the day files (so the entry-day coverage guard is not what fires)
    # but empty them of this panel's tickers: every row now drops for want of
    # an entry price and the date re-prices to zero kept rows.
    for day in DAYS:
        month_dir = vault_root / "data" / "market_eod" / day.strftime("%Y-%m")
        path = month_dir / f"{day.isoformat()}.jsonl.gz"
        if path.is_file():
            path.write_bytes(
                _gz_jsonl(
                    [
                        {
                            "symbol": "ZZZZ",
                            "close": 1.0,
                            "open": 1.0,
                            "high": 1.0,
                            "low": 1.0,
                            "volume": 1.0,
                            "vwap": 1.0,
                            "transactions": 1,
                        }
                    ]
                )
            )
    from tests.test_vault import _manifest

    for day in DAYS:
        month_dir = vault_root / "data" / "market_eod" / day.strftime("%Y-%m")
        if month_dir.is_dir():
            _manifest(month_dir, sorted(p.name for p in month_dir.glob("*.jsonl.gz")))

    vault = VaultDataSource(vault_root)
    result, parts = build_signal_panel(vault, "unit_signal", config=SignalPanelConfig(rebuild=True))
    with pytest.raises(SignalPanelError, match="re-priced to zero kept rows"):
        write_signal_panel(vault, "unit_signal", result, parts)
    # The closed part is still there: refusing never destroys evidence.
    assert (panel_path.parent / f"{SIGNAL.isoformat()}.parquet").is_file()


def test_a_signal_date_whose_entry_day_is_unarchived_refuses(vault_root: Path) -> None:
    """Losing 100% of a period used to be silent, and it shrinks the denominator.

    Entry pricing needs an exact bar on the entry day. With the day absent every
    observation dropped, no part was written, and the period vanished from
    panel.parquet — contributing 0 to every numerator AND denominator while the
    survivors attested perfectly. `if not needed_days` never fired because the
    other windows were covered.
    """
    later = [
        _observation(
            name,
            float(i),
            float(i) / 10.0,
            signal=LATER_SIGNAL,
            entry=LATER_ENTRY,
            exit_=LATER_EXIT,
        )
        for i, name in enumerate(["ALPHA", "BETA", "GAMMA", "DELTA"], start=1)
    ]
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows(), LATER_SIGNAL: later})
    # Drop exactly the later window's entry day.
    month_dir = vault_root / "data" / "market_eod" / LATER_ENTRY.strftime("%Y-%m")
    (month_dir / f"{LATER_ENTRY.isoformat()}.jsonl.gz").unlink()
    from tests.test_vault import _manifest

    _manifest(month_dir, sorted(p.name for p in month_dir.glob("*.jsonl.gz")))

    result, panel_path = _build(vault_root)
    assert panel_path is None
    assert result.refusal is not None
    assert "no bar on the entry day" in result.refusal
    assert LATER_SIGNAL.isoformat() in result.refusal


def test_unresolved_ticker_identities_are_whole_panel_not_last_run(
    vault_root: Path,
) -> None:
    """build.json labelled this field whole-panel while it was last-run-only.

    An incremental run re-prices nothing, so the list emptied while its labelled
    neighbour unresolved_rows stayed non-zero — an affirmative "no repeat
    offenders" beside a count saying otherwise.
    """
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    first, panel_path = _build(vault_root)
    assert first.unresolved_rows > 0
    assert first.unresolved_tickers

    second, _ = _build(vault_root)
    assert second.parts_written == 0  # nothing re-priced
    assert second.unresolved_rows == first.unresolved_rows
    assert second.unresolved_tickers == first.unresolved_tickers
    build = json.loads((panel_path.parent / "build.json").read_text())
    assert build["unresolved_tickers"] == first.unresolved_tickers
    assert build["unresolved_tickers_incomplete_dates"] == []
    counts = json.loads((panel_path.parent / "counts.json").read_text())
    assert counts[SIGNAL.isoformat()]["unresolved_tickers"]


def test_survival_and_period_coverage_reach_the_sidecar(vault_root: Path) -> None:
    """`no_start_price_dropped` reached no fraction, no gate, no attestation."""
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    result, panel_path = _build(vault_root)
    build = json.loads((panel_path.parent / "build.json").read_text())
    assert build["panel_observations"] == result.panel_observations > 0
    assert build["no_start_price_rows"] >= 1  # NOENTRY has no entry bar
    assert 0.0 < build["survival_rate"] < 1.0
    assert build["periods_in_panel"] == 1
    assert build["periods_accounted"] == 1


def test_counts_json_survives_a_crash_between_it_and_the_parts(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering + atomicity: counts may run AHEAD of parts, never behind.

    A part on disk with no counts entry is unrecoverable — the pending filter
    keys off part existence, so that date is never re-priced and `.get(...,0)`
    reads the hole as zero unresolved drops. Counts ahead of parts self-heals.
    """
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    vault = VaultDataSource(vault_root)
    result, parts = build_signal_panel(vault, "unit_signal", config=SignalPanelConfig())

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", _boom)
    with pytest.raises(OSError, match="disk full"):
        write_signal_panel(vault, "unit_signal", result, parts)
    monkeypatch.undo()

    out_dir = vault_root / "data" / "signal_panels" / "unit_signal" / f"v{result.panel_version}"
    counts = json.loads((out_dir / "counts.json").read_text())
    assert SIGNAL.isoformat() in counts  # accounting landed first
    assert not list(out_dir.glob("*.tmp"))  # and atomically
    assert not (out_dir / f"{SIGNAL.isoformat()}.parquet").is_file()

    # The next run re-prices the date and the two agree again.
    healed, panel_path = _build(vault_root)
    assert healed.parts_written == 1
    assert len(pd.read_parquet(panel_path)) == healed.kept_rows


def test_a_caller_supplied_license_note_cannot_drop_the_returns_provenance(
    vault_root: Path,
) -> None:
    """`+` binds tighter than `or`: the override discarded the whole clause."""
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    vault = VaultDataSource(vault_root)
    result, parts = build_signal_panel(vault, "unit_signal", config=SignalPanelConfig())
    panel_path = write_signal_panel(
        vault,
        "unit_signal",
        result,
        parts,
        license_note="FINRA short interest, internal use only",
    )
    manifest = json.loads((panel_path.parent / "manifest.json").read_text())
    note = manifest["license_note"]
    assert "FINRA short interest, internal use only" in note
    assert "Massive" in note and "stockanalysis.com" in note
    assert "do not redistribute rows" in note


# ============================================================ 2. split guard


def _bars(rows: list[tuple[dt.date, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": day,
                "ticker": "SUBSPLIT",
                "symbol": "SUBSPLIT",
                "close": close,
                "volume": 1e5,
                "transactions": 1000.0,
            }
            for day, close in rows
        ]
    )


def _foundry(day: dt.date, ratio: float) -> pd.DataFrame:
    return pd.DataFrame(
        [{"ticker": "SUBSPLIT", "cik": "0000000042", "effective_date": day, "ratio": ratio}]
    )


ENTRY_D = dt.date(2026, 8, 4)
SPLIT_D = dt.date(2026, 8, 5)
EXIT_D = dt.date(2026, 8, 6)


@pytest.mark.parametrize(
    ("before", "after", "ratio"),
    [
        (40.0, 32.0, 1.25),  # 5:4 forward
        (60.0, 50.0, 1.2),  # 6:5 forward
        (40.0, 50.0, 0.8),  # 1.25:1 reverse
    ],
)
def test_a_sub_1_5_foundry_split_is_detected_not_merely_confirmed(
    before: float, after: float, ratio: float
) -> None:
    """PLAUSIBLE_SPLIT_RATIOS floors at 1.5, and the foundry was gated behind it.

    So the authoritative table — which carries effective_date and ratio for
    every split — was never read unless the price signature had already guessed
    one. Everything between 1/1.5 and 1.5 was invisible in both directions: the
    row survived with split_factor=1.0 and a fabricated forward return.
    """
    bars = _bars([(ENTRY_D, before), (SPLIT_D, after), (EXIT_D, after)])
    factor, source, unresolved = split_factor(
        bars,
        ENTRY_D,
        EXIT_D,
        ticker="SUBSPLIT",
        cik="0000000042",
        foundry_splits=_foundry(SPLIT_D, ratio),
        tolerance=0.01,
    )
    assert unresolved is False
    assert source == "foundry"
    assert factor == pytest.approx(ratio)
    # The whole point: the return is no longer fabricated.
    assert (after * factor) / before - 1.0 == pytest.approx(0.0, abs=1e-9)


def test_a_foundry_split_the_price_contradicts_is_unresolved() -> None:
    """Applying it unconditionally would fabricate the return the other way."""
    bars = _bars([(ENTRY_D, 40.0), (SPLIT_D, 40.0), (EXIT_D, 40.0)])  # flat
    factor, source, unresolved = split_factor(
        bars,
        ENTRY_D,
        EXIT_D,
        ticker="SUBSPLIT",
        cik="0000000042",
        foundry_splits=_foundry(SPLIT_D, 1.25),
        tolerance=0.01,
    )
    assert unresolved is True
    assert (factor, source) == (1.0, "none")


def test_an_implausible_foundry_ratio_is_unresolved_never_ignored() -> None:
    """splits.jsonl carries parse artifacts spanning ~3e-06 to ~1.1e8.

    A ratio no return may be multiplied by is missing information, not 1.0.
    """
    bars = _bars([(ENTRY_D, 40.0), (SPLIT_D, 32.0), (EXIT_D, 32.0)])
    _, _, unresolved = split_factor(
        bars,
        ENTRY_D,
        EXIT_D,
        ticker="SUBSPLIT",
        cik="0000000042",
        foundry_splits=_foundry(SPLIT_D, 1.09e8),
        tolerance=0.01,
    )
    assert unresolved is True


def test_a_split_outside_the_window_is_left_alone() -> None:
    """(entry, exit] is the window; a split before entry is already in the basis."""
    bars = _bars([(ENTRY_D, 40.0), (SPLIT_D, 40.0), (EXIT_D, 40.0)])
    factor, source, unresolved = split_factor(
        bars,
        ENTRY_D,
        EXIT_D,
        ticker="SUBSPLIT",
        cik="0000000042",
        foundry_splits=_foundry(dt.date(2026, 7, 1), 1.25),
        tolerance=0.01,
    )
    assert (factor, source, unresolved) == (1.0, "none", False)


class _FoundryStub:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def splits(self) -> pd.DataFrame:
        return self._frame


def test_dividend_basis_is_unsafe_for_a_split_of_any_ratio(vault_root: Path) -> None:
    """The dividend leg keyed off `factor != 1.0`.

    A 5:4 split the price signature cannot see left the factor at 1.0, so the
    row was ALSO declared dividend-covered and pre-split cash was added on a
    post-split share basis. The condition has to be "a split event occurred in
    (entry, exit]", which is exactly what split_source now reports.
    """
    from tests.test_vault import _manifest

    # A name with BOTH a sub-1.5 split and a cash dividend inside the window.
    for day in DAYS:
        month_dir = vault_root / "data" / "market_eod" / day.strftime("%Y-%m")
        path = month_dir / f"{day.isoformat()}.jsonl.gz"
        if not path.is_file():
            continue
        import gzip

        rows = [json.loads(x) for x in gzip.decompress(path.read_bytes()).decode().splitlines()]
        close = 40.0 if day < dt.date(2026, 8, 5) else 32.0
        rows.append(
            {
                "symbol": "SPLITDIV",
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1e5,
                "vwap": close,
                "transactions": 1000.0,
            }
        )
        path.write_bytes(_gz_jsonl(rows))
    for day in DAYS:
        month_dir = vault_root / "data" / "market_eod" / day.strftime("%Y-%m")
        if month_dir.is_dir():
            _manifest(month_dir, sorted(x.name for x in month_dir.glob("*.jsonl.gz")))

    dividends = vault_root / "data" / "dividends" / "2026-08"
    (dividends / "2026-08.jsonl.gz").write_bytes(
        _gz_jsonl(
            [
                {
                    "ticker": "DIVCO",
                    "ex_dividend_date": "2026-08-06",
                    "cash_amount": 2.0,
                    "currency": "USD",
                    "dividend_type": "CD",
                },
                {
                    "ticker": "SPLITDIV",
                    "ex_dividend_date": "2026-08-06",
                    "cash_amount": 1.0,
                    "currency": "USD",
                    "dividend_type": "CD",
                },
            ]
        )
    )
    _manifest(dividends, ["2026-08.jsonl.gz"])

    rows = _hazard_rows()
    rows.append(_observation("SPLITDIV", 30.0, 0.95))
    _write_observations(vault_root, "unit_signal", {SIGNAL: rows})

    cik = next(r for r in rows if r["ticker"] == "SPLITDIV")["cik"]
    foundry = _FoundryStub(
        pd.DataFrame(
            [
                {
                    "ticker": "SPLITDIV",
                    "cik": cik,
                    "effective_date": dt.date(2026, 8, 5),
                    "ratio": 1.25,
                }
            ]
        )
    )
    vault = VaultDataSource(vault_root)
    result, parts = build_signal_panel(
        vault, "unit_signal", foundry=foundry, config=SignalPanelConfig()
    )
    panel_path = write_signal_panel(vault, "unit_signal", result, parts)
    panel = pd.read_parquet(panel_path)
    row = panel[panel["ticker"] == "SPLITDIV"].iloc[0]

    assert row["split_source"] == "foundry"
    assert row["split_factor"] == pytest.approx(1.25)
    # 40 -> 32 with a 5:4 split is FLAT, not -20%.
    assert row["forward_return"] == pytest.approx(0.0, abs=1e-9)
    # And the share basis for the in-window cash is unknowable, so no dividend
    # cash was added and the row is honestly uncovered.
    assert bool(row["dividend_covered"]) is False
    assert row["dividend_cash"] == pytest.approx(0.0)


# ================================================================ 3. ledger


def _record(experiment: str, sharpe: float | None = None):
    from stock_grader.research_manifest import ResearchRecord

    return ResearchRecord(
        experiment=experiment,
        market="us_equities",
        symbols=[],
        targets=["forward_return"],
        horizons=[21],
        trials=1,
        metrics={} if sharpe is None else {"per_period_sharpe": sharpe},
        costs={},
        benchmark="zero",
        leakage_controls="n/a",
        gate_passed=False,
        verdict="unit",
    )


def test_append_record_returns_the_record_that_was_written(tmp_path: Path) -> None:
    """prev_sha256 is inside payload(), so the pre-append hash is never on disk.

    Reporting it hands an operator an evidence pointer that resolves to nothing
    and that `ledger-retract --sha256` rejects as "not in the ledger".
    """
    from stock_grader.research_manifest import append_record, load_manifest

    ledger = tmp_path / "ledger.jsonl"
    record = _record("unit:one")
    written = append_record(ledger, record)
    on_disk = str(load_manifest(ledger)[-1]["integrity_sha256"])
    assert written.integrity_sha256() == on_disk
    assert record.integrity_sha256() != on_disk  # the bug, pinned


def test_a_tampered_line_cannot_set_the_deflation_dispersion(tmp_path: Path) -> None:
    """`find_preregistration` and `_promotion_declarations` both refuse a line
    that does not hash to its own claim; the trial denominator did not."""
    from stock_grader.research_manifest import (
        append_record,
        load_manifest,
        trial_sharpes_by_experiment,
    )

    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, _record("unit:a", 0.10))
    append_record(ledger, _record("unit:b", 0.20))
    lines = ledger.read_text().splitlines()
    forged = json.loads(lines[1])
    forged["metrics"]["per_period_sharpe"] = 99.0  # integrity_sha256 left intact
    lines[1] = json.dumps(forged)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sharpes = trial_sharpes_by_experiment(load_manifest(ledger))
    assert "unit:b" not in sharpes
    assert sharpes == {"unit:a": pytest.approx(0.10)}


@pytest.mark.parametrize("evidence", [[""], ["not-a-hash"], [0], ["AB" * 32]])
def test_evidence_pointers_must_be_ledger_line_hashes(evidence: list) -> None:
    """A length-only check let [""] satisfy "names the records it rests on"."""
    from stock_grader.research_manifest import (
        promotion_policy_declaration,
        promotion_policy_record,
        spec_sha256,
        validate_promotion_transition,
    )

    declaration = promotion_policy_declaration(
        policy_version="p1", policy_doc="d.md", policy_sha256="ab" * 32
    )
    record = promotion_policy_record(declaration)
    line = json.loads(record.to_line())
    transition = {
        "kind": "stage-transition",
        "policy_version": "p1",
        "policy_sha256": "ab" * 32,
        "subject_spec_sha256": "cd" * 32,
        "from_stage": "exploratory",
        "to_stage": "declared_trial",
        "evidence_sha256": evidence,
        "reason": "unit",
    }
    assert spec_sha256(declaration) == line["symbols"][0]
    error = validate_promotion_transition([line], transition)
    assert error is not None and "malformed evidence" in error


def test_promotion_stage_seeds_from_the_declared_ladder() -> None:
    """PROMOTION.md: transitions are validated against the policy AS DECLARED,
    not against code constants. The starting rung was the one exception, which
    made any policy with a different bottom rung unenterable."""
    from stock_grader.research_manifest import promotion_stage

    assert promotion_stage([], "cd" * 32) == "exploratory"
    assert promotion_stage([], "cd" * 32, stages=["candidate", "declared_trial"]) == "candidate"


# ============================================================= 3b. ledger CLI


def _policy_doc(tmp_path: Path, version: str = "promotion-policy-v1") -> Path:
    doc = tmp_path / "POLICY.md"
    doc.write_text(f"Version string: {version}\nretired is terminal.\n", encoding="utf-8")
    return doc


def _declare_policy(tmp_path: Path, ledger: Path, doc: Path, version: str, *extra: str) -> int:
    return cli.main(
        [
            "promotion-declare",
            "--ledger",
            str(ledger),
            "--policy-doc",
            str(doc),
            "--policy-version",
            version,
            *extra,
        ]
    )


def test_promotion_declare_prints_the_hash_the_ledger_holds(tmp_path, capsys) -> None:
    from stock_grader.research_manifest import load_manifest

    ledger = tmp_path / "ledger.jsonl"
    assert _declare_policy(tmp_path, ledger, _policy_doc(tmp_path), "promotion-policy-v1") == 0
    printed = _flat(capsys.readouterr().out)
    on_disk = str(load_manifest(ledger)[-1]["integrity_sha256"])
    assert on_disk[:12] in printed


def test_promotion_declare_refuses_a_document_that_does_not_name_the_version(
    tmp_path, capsys
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    doc = _policy_doc(tmp_path, "promotion-policy-v1")
    assert _declare_policy(tmp_path, ledger, doc, "promotion-policy-v2") == 2
    assert _flat("does not contain the string") in _flat(capsys.readouterr().out)


def test_a_new_version_cannot_be_bound_to_the_unchanged_document(tmp_path, capsys) -> None:
    """The reproduced live-money hole: v2 declared against v1's own bytes.

    The one-directional guard refused "same version, changed doc" and allowed
    its mirror image, so `--live-money-reachable` could open the money rung
    while the very text it cited said the rung was unreachable.
    """
    ledger = tmp_path / "ledger.jsonl"
    doc = tmp_path / "POLICY.md"
    doc.write_text(
        "Version string: promotion-policy-v1\nAlso mentions promotion-policy-v2.\n",
        encoding="utf-8",
    )
    assert _declare_policy(tmp_path, ledger, doc, "promotion-policy-v1") == 0
    assert (
        _declare_policy(tmp_path, ledger, doc, "promotion-policy-v2", "--live-money-reachable") == 2
    )
    assert _flat("is already declared as") in _flat(capsys.readouterr().out)


def test_retracting_a_promotion_record_is_refused(tmp_path, capsys) -> None:
    """Retraction is trial accounting. A promotion record carries trials=0 and
    no metrics, so retracting one has no accounting effect at all — only the
    lifecycle side effect of rewinding `promotion_stage` to a rung the subject
    already left, with none of the reason/evidence a demotion requires."""
    from stock_grader.research_manifest import load_manifest, promotion_stage

    ledger = tmp_path / "ledger.jsonl"
    doc = _policy_doc(tmp_path)
    assert _declare_policy(tmp_path, ledger, doc, "promotion-policy-v1") == 0
    subject = "ab" * 32
    assert (
        cli.main(
            [
                "promotion-declare",
                "--ledger",
                str(ledger),
                "--policy-doc",
                str(doc),
                "--policy-version",
                "promotion-policy-v1",
                "--subject",
                subject,
                "--from-stage",
                "exploratory",
                "--to-stage",
                "declared_trial",
                "--evidence",
                "cd" * 32,
                "--reason",
                "unit",
            ]
        )
        == 0
    )
    records = load_manifest(ledger)
    assert promotion_stage(records, subject) == "declared_trial"
    transition_hash = str(records[-1]["integrity_sha256"])
    capsys.readouterr()

    assert (
        cli.main(
            [
                "ledger-retract",
                transition_hash,
                "--ledger",
                str(ledger),
                "--reason",
                "changed my mind",
            ]
        )
        == 2
    )
    assert _flat("refusing to retract a ledger:promotion record") in _flat(capsys.readouterr().out)
    assert promotion_stage(load_manifest(ledger), subject) == "declared_trial"


def test_backtest_refuses_to_evaluate_against_a_broken_chain(tmp_path, capsys) -> None:
    """Every APPENDING verb refused a broken chain; the two verbs that also
    CONSUME the trial denominator did not."""
    from stock_grader.research_manifest import append_record

    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, _record("unit:a", 0.10))
    append_record(ledger, _record("unit:b", 0.20))
    lines = ledger.read_text().splitlines()
    forged = json.loads(lines[0])
    forged["metrics"]["per_period_sharpe"] = 42.0
    lines[0] = json.dumps(forged)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    panel = tmp_path / "panel.csv"
    pd.DataFrame(
        [
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
    ).to_csv(panel, index=False)

    assert (
        cli.main(
            [
                "backtest",
                str(panel),
                "--ledger",
                str(ledger),
                "--quantiles",
                "2",
                "--min-cross-section",
                "10",
                "--allow-unverified-panel",
                "--allow-unmanifested-panel",
            ]
        )
        == 2
    )
    assert _flat("does not verify") in _flat(capsys.readouterr().out)
    assert len(ledger.read_text().splitlines()) == 2  # nothing appended


# ================================================== 4. frozen-panel catalog


def test_refresh_refuses_to_rebless_a_mutated_part(tmp_path: Path) -> None:
    """The producer laundered exactly what the consumer refuses.

    `verify_sibling_manifest` calls a changed part "corruption or tampering,
    never a version bump"; `refresh_frozen_manifest` re-hashed it, wrote the new
    digest with hashed_at="backfill", and the next verify came back clean.
    """
    from stock_grader.frozen_manifest import (
        refresh_frozen_manifest,
        verify_sibling_manifest,
    )

    directory = tmp_path / "frozen" / "all_weather"
    directory.mkdir(parents=True)
    part = directory / "2026-08-01.parquet"
    pd.DataFrame({"ticker": ["A", "B"], "score": [1.0, 2.0]}).to_parquet(part, index=False)
    refresh_frozen_manifest(directory, frozen_now=frozenset({part.name}))
    assert verify_sibling_manifest(part) is True

    pd.DataFrame({"ticker": ["A", "B"], "score": [999.0, 2.0]}).to_parquet(part, index=False)
    with pytest.raises(ValueError, match="corruption or tampering"):
        refresh_frozen_manifest(directory)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        verify_sibling_manifest(part)  # the alarm still stands


def test_refresh_refuses_an_unreadable_or_foreign_catalog(tmp_path: Path) -> None:
    """`_prior_entries` returned {} for a manifest it could not read, and
    `_write_if_changed` then overwrote it — destroying the catalog the verifier
    refuses and rebuilding it over whatever bytes were present."""
    from stock_grader.frozen_manifest import refresh_frozen_manifest

    directory = tmp_path / "frozen" / "all_weather"
    directory.mkdir(parents=True)
    part = directory / "2026-08-01.parquet"
    pd.DataFrame({"ticker": ["A"], "score": [1.0]}).to_parquet(part, index=False)

    (directory / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be parsed"):
        refresh_frozen_manifest(directory)

    (directory / "manifest.json").write_text(
        json.dumps({"schema_version": "2.0", "files": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="schema_version"):
        refresh_frozen_manifest(directory)


def test_strict_verification_refuses_a_missing_manifest(tmp_path: Path) -> None:
    """Deleting one file is not supposed to be stronger than editing a hash."""
    from stock_grader.frozen_manifest import (
        UnmanifestedPanelWarning,
        verify_sibling_manifest,
    )

    directory = tmp_path / "frozen" / "all_weather"
    directory.mkdir(parents=True)
    part = directory / "2026-08-01.parquet"
    pd.DataFrame({"ticker": ["A"], "score": [1.0]}).to_parquet(part, index=False)

    with pytest.warns(UnmanifestedPanelWarning):
        assert verify_sibling_manifest(part) is False
    with pytest.raises(ValueError, match="requires attestation"):
        verify_sibling_manifest(part, strict=True)


# ============================================== 5. cadence clocks + workflow


def test_every_committed_evidence_root_is_clocked(tmp_path: Path) -> None:
    """monthly-freeze writes frozen_scores AND frozen_scores_wide and commits
    both. The clock watched one, so half the forward evidence had exactly the
    property this module exists to eliminate."""
    assert "frozen_scores_wide" in cadence.FROZEN_ROOTS

    root = tmp_path / "repo"
    for base, day in (("frozen_scores", "2026-08-01"), ("frozen_scores_wide", "2026-07-31")):
        directory = root / base / "all_weather"
        directory.mkdir(parents=True)
        (directory / f"{day}.parquet").write_bytes(b"")
    (root / "docs" / "forward" / "2026-08").mkdir(parents=True)
    (root / "docs" / "forward" / "2026-08" / "accounting.json").write_text(
        json.dumps({"schema_version": "1.0", "month": "2026-08", "runs": []}),
        encoding="utf-8",
    )

    ok, lines = cadence.check_cadence(root, dt.date(2026, 8, 4))
    assert ok is False
    assert any(line.startswith("PASS freeze[frozen_scores]") for line in lines)
    assert any(line.startswith("FAIL freeze[frozen_scores_wide]") for line in lines)


def test_the_declared_look_schedule_names_the_dispatch_surface() -> None:
    """workflow_dispatch runs the identical ledger-appending path, and
    `ledger-declare` is idempotent, so the string cannot be amended later.
    Stock-Vault's scoreboard and docs/PROMOTION.md both name it; this did not."""
    text = FORWARD_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    schedule = [line for line in text.splitlines() if "--schedule" in line]
    assert schedule, "the pre-registered look schedule vanished"
    assert all("workflow_dispatch" in line for line in schedule)


def test_dated_forward_reports_are_not_written_by_a_bare_redirect() -> None:
    """`>` truncates BEFORE the command runs, so a failing run on the 6th
    destroyed a succeeding run's report from the 4th and committed a 0-byte
    evidence artifact. Dated artifacts are immutable everywhere else."""
    text = FORWARD_WORKFLOW.read_text(encoding="utf-8")
    assert '--format md > "$OUT/$p.md"' not in text
    assert 'cp "build/panels/$p.build.json" "$OUT/$p.build.json"' not in text
    assert "_immutable_place" in text
    assert "dated forward artifacts are immutable" in text


def test_an_unattested_frozen_input_blocks_the_forward_panel(tmp_path: Path) -> None:
    """`verify_sibling_manifest`'s return value was discarded at every call site.

    A missing manifest warned (invisibly, on stderr, in a green CI run) and the
    panel built anyway, producing a sidecar and a ledger line byte-identical in
    shape to a hash-verified build. The boolean now reaches the sidecar and
    gates ready_for_backtest, which is what the monthly workflow reads.
    """
    from tests.test_frozen_manifest import _frozen_profile_dir
    from tests.test_panel import TODAY, _build_market_vault, _config, _Foundry

    from stock_grader.frozen_manifest import (
        UnmanifestedPanelWarning,
        refresh_frozen_manifest,
    )
    from stock_grader.panel import build_panel

    root = tmp_path / "frozen"
    directory = _frozen_profile_dir(root)
    vault = VaultDataSource(_build_market_vault(tmp_path / "vault"))

    refresh_frozen_manifest(directory)
    attested = build_panel(root, "all_weather", vault, _Foundry(), _config(), today=TODAY)
    assert attested.refusal is None
    assert attested.frozen_inputs_attested is True
    assert attested.ready_for_backtest is True

    (directory / "manifest.json").unlink()  # one deleted file, no edited hash
    with pytest.warns(UnmanifestedPanelWarning):
        bare = build_panel(root, "all_weather", vault, _Foundry(), _config(), today=TODAY)
    assert bare.refusal is None  # pre-convention reads still work...
    assert bare.frozen_inputs_attested is False  # ...but they say so
    assert bare.ready_for_backtest is False
    assert bare.qualifying_periods == attested.qualifying_periods
