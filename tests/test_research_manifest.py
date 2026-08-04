"""Tamper-evidence of the append-only research ledger: per-line hashes + chain.

A per-line hash proves each surviving line is intact; only the prev_sha256
chain proves no line was quietly deleted or reordered. Legacy lines written
before chaining existed must keep verifying unchanged.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from pathlib import Path

from stock_grader.research_manifest import (
    GENESIS_SHA256,
    ResearchRecord,
    append_record,
    load_manifest,
    summarize_manifest,
    verify_chain,
    verify_line,
)


def _record(experiment: str = "unit") -> ResearchRecord:
    return ResearchRecord(
        experiment=experiment,
        market="us_equities",
        symbols=["XYZ"],
        targets=["forward_return"],
        horizons=[21],
        trials=1,
        metrics={"per_period_sharpe": 0.1},
        costs={"transaction_cost_bps": 10.0},
        benchmark="zero",
        leakage_controls="unit test",
        gate_passed=False,
        verdict="NO EDGE",
    )


def _ledger(tmp_path, names: tuple[str, ...] = ("a", "b", "c")) -> Path:
    path = tmp_path / "ledger.jsonl"
    for name in names:
        append_record(path, _record(name))
    return path


def test_append_chains_each_record_to_the_previous_line(tmp_path) -> None:
    records = load_manifest(_ledger(tmp_path))

    assert records[0]["prev_sha256"] == GENESIS_SHA256
    for previous, current in pairwise(records):
        assert current["prev_sha256"] == previous["integrity_sha256"]
    assert all(verify_line(record) for record in records)
    assert verify_chain(records)
    summary = summarize_manifest(records)
    assert summary["chain_ok"]
    assert summary["all_integrity_ok"]


def test_deleting_a_middle_chained_line_is_detected(tmp_path) -> None:
    path = _ledger(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    records = load_manifest(path)
    # Every surviving line still hashes cleanly -- exactly why per-line hashes
    # alone were not tamper evidence for deletion.
    assert all(verify_line(record) for record in records)
    assert not verify_chain(records)
    assert not summarize_manifest(records)["all_integrity_ok"]


def test_reordering_chained_lines_is_detected(tmp_path) -> None:
    records = load_manifest(_ledger(tmp_path))
    reordered = [records[0], records[2], records[1]]

    assert all(verify_line(record) for record in reordered)
    assert not verify_chain(reordered)


def test_modifying_a_chained_line_is_detected(tmp_path) -> None:
    records = load_manifest(_ledger(tmp_path))
    doctored = dict(records[1])
    doctored["verdict"] = "EDGE"

    # Naive edit: the line no longer matches its own hash.
    assert not verify_chain([records[0], doctored, records[2]])

    # Sophisticated edit: recompute the line hash over the doctored payload.
    # The line then verifies in isolation, but the next line's prev_sha256
    # still binds to the original hash, so the chain exposes it.
    payload = {k: v for k, v in doctored.items() if k != "integrity_sha256"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    doctored["integrity_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert verify_line(doctored)
    assert not verify_chain([records[0], doctored, records[2]])


def test_legacy_prefix_without_prev_sha256_still_verifies(tmp_path) -> None:
    path = tmp_path / "ledger.jsonl"
    # A pre-chain writer: the record carries no prev_sha256 and its hash covers
    # exactly the legacy payload.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_record("legacy").to_line() + "\n")
    append_record(path, _record("chained"))

    records = load_manifest(path)
    assert "prev_sha256" not in records[0]
    assert records[1]["prev_sha256"] == records[0]["integrity_sha256"]
    assert all(verify_line(record) for record in records)
    assert verify_chain(records)
    assert summarize_manifest(records)["all_integrity_ok"]


def test_unchained_line_after_the_chain_begins_is_a_splice(tmp_path) -> None:
    records = load_manifest(_ledger(tmp_path, names=("a", "b")))
    spliced = dict(_record("spliced").payload())
    canonical = json.dumps(spliced, sort_keys=True, separators=(",", ":"))
    spliced["integrity_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert verify_line(spliced)
    assert not verify_chain([*records, spliced])


def _synthetic(experiment, sharpe, **kw):
    from stock_grader.research_manifest import ResearchRecord

    return ResearchRecord(
        experiment=experiment, market="us_equities", symbols=kw.get("symbols", []),
        targets=[], horizons=[1], trials=1, metrics={"per_period_sharpe": sharpe},
        costs={}, benchmark="none", leakage_controls="PASS", gate_passed=False,
        verdict="NO EDGE",
    )


def test_retracted_records_are_excluded_from_trial_accounting(tmp_path):
    """A retracted trial must stop deflating every future result.

    Twelve synthetic CLI-test panels sit in the live ledger with an identical
    Sharpe of 2.83. The ledger is append-only, so they cannot be deleted — a
    later record names them and the collector skips them.
    """
    from stock_grader.research_manifest import (
        RETRACTION_EXPERIMENT,
        append_record,
        load_manifest,
        retracted_hashes,
        trial_sharpes,
        verify_chain,
    )

    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, _synthetic("backtest:synthetic", 2.8284271247461894))
    append_record(ledger, _synthetic("backtest:real", 0.31))
    records = load_manifest(ledger)
    assert sorted(trial_sharpes(records)) == [0.31, 2.8284271247461894]

    junk = records[0]["integrity_sha256"]
    append_record(
        ledger,
        _synthetic(RETRACTION_EXPERIMENT, None, symbols=[junk]),
    )
    records = load_manifest(ledger)

    assert retracted_hashes(records) == {junk}
    assert trial_sharpes(records) == [0.31], "the retracted trial must not deflate"
    # The retraction is itself chained, so the exclusion stays auditable.
    assert verify_chain(records)
    assert len(records) == 3, "retraction appends; it never removes"


def test_repeated_measurement_of_one_experiment_is_one_trial(tmp_path):
    """Re-running one profile monthly is that trial measured again, not a new one.

    Counting each month separately inflates the deflation benchmark until no
    real edge could clear it.
    """
    from stock_grader.research_manifest import append_record, load_manifest, trial_sharpes

    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, _synthetic("backtest:all_weather", 0.20))
    append_record(ledger, _synthetic("backtest:all_weather", 0.28))
    append_record(ledger, _synthetic("backtest:all_weather", 0.33))
    append_record(ledger, _synthetic("backtest:value", 0.11))

    sharpes = trial_sharpes(load_manifest(ledger))
    assert sorted(sharpes) == [0.11, 0.33], "collapse to the latest look per experiment"


_SPEC = {
    "kind": "backtest_panel",
    "profiles": ["all_weather"],
    "config_fingerprints": ["1790775d"],
    "universe_ids": [],
    "horizon_days": [21],
    "targets": ["forward_return"],
    "quantiles": 5,
    "min_cross_section": 20,
    "periods_per_year": 12,
    "transaction_cost_bps": 10.0,
}


def test_preregistration_declaration_is_chained_and_never_a_trial(tmp_path):
    from stock_grader.research_manifest import (
        PREREGISTRATION_EXPERIMENT,
        append_record,
        find_preregistration,
        load_manifest,
        preregistration_record,
        spec_sha256,
        trial_sharpes,
        verify_chain,
    )

    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, _synthetic("backtest:something_else", 0.31))
    append_record(
        ledger, preregistration_record(_SPEC, schedule="monthly (cron 41 2 6 * *)")
    )
    records = load_manifest(ledger)

    assert verify_chain(records), "a declaration must extend the chain, never break it"
    declaration = records[-1]
    assert declaration["experiment"] == PREREGISTRATION_EXPERIMENT
    assert declaration["symbols"] == [spec_sha256(_SPEC)]
    assert "monthly (cron 41 2 6 * *)" in declaration["verdict"]
    assert "disclosed peeking, not corrected" in declaration["verdict"]
    # No metrics: the declaration itself must never enter the denominator.
    assert trial_sharpes(records) == [0.31]
    found = find_preregistration(records, _SPEC)
    assert found is not None
    assert found["integrity_sha256"] == declaration["integrity_sha256"]


def test_preregistration_spec_mismatch_or_tamper_refuses_match(tmp_path):
    from stock_grader.research_manifest import (
        append_record,
        find_preregistration,
        load_manifest,
        preregistration_record,
    )

    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, preregistration_record(_SPEC, schedule="monthly"))
    records = load_manifest(ledger)

    # A different hypothesis (changed scoring config) must not match.
    drifted = dict(_SPEC, config_fingerprints=["751441e6"])
    assert find_preregistration(records, drifted) is None

    # A lying declaration: claimed hash and stored spec disagree. It was
    # honestly appended (chain intact), but it must not bless anything.
    lying = preregistration_record(drifted, schedule="monthly")
    from dataclasses import replace

    from stock_grader.research_manifest import spec_sha256

    lying = replace(lying, symbols=[spec_sha256(_SPEC)])
    append_record(ledger, lying)
    tampered_records = load_manifest(ledger)
    # The lying line claims _SPEC's hash, but its stored spec re-hashes to the
    # drifted spec — the match must be refused even though the newest claim wins
    # ordinarily.
    assert (
        find_preregistration(tampered_records, _SPEC)["integrity_sha256"]
        == tampered_records[0]["integrity_sha256"]
    ), "the honest declaration must win; the lying one is treated as absent"


def test_retracted_declaration_no_longer_matches(tmp_path):
    from stock_grader.research_manifest import (
        RETRACTION_EXPERIMENT,
        append_record,
        find_preregistration,
        load_manifest,
        preregistration_record,
    )

    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, preregistration_record(_SPEC, schedule="monthly"))
    declared = load_manifest(ledger)[0]["integrity_sha256"]
    append_record(ledger, _synthetic(RETRACTION_EXPERIMENT, None, symbols=[declared]))

    records = load_manifest(ledger)
    assert find_preregistration(records, _SPEC) is None


def test_preregistered_experiment_name_is_spec_bound_not_filename_bound():
    from stock_grader.research_manifest import preregistered_experiment, spec_sha256

    name = preregistered_experiment(_SPEC)
    assert name.startswith("backtest:preregistered:all_weather:")
    assert spec_sha256(_SPEC)[:12] in name
    # Any spec change renames the experiment: a new configuration is a new trial.
    assert preregistered_experiment(dict(_SPEC, quantiles=4)) != name
