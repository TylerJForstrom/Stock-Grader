"""Sibling manifests: the catalog written at freeze, verified at every read.

Covers both halves of the boundary this module closes: the producer
(``cmd_freeze`` / ``write_panel`` write ``manifest.json`` beside the parts)
and the consumers (``backtest``'s panel loader, ``build_panel``, and the decay
loader refuse a part whose bytes the sibling catalog does not vouch for,
while directories that predate the convention warn and stay readable).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from tests.test_cli import _freeze_args, _patch_freeze_universe
from tests.test_panel import (
    SIGNALS,
    TODAY,
    _build_market_vault,
    _config,
    _Foundry,
    _healthy_rows,
    _write_frozen,
)
from tests.test_pipeline import _universe

from stock_grader import cli
from stock_grader.data.vault import VaultDataSource
from stock_grader.decay import load_frozen_panels
from stock_grader.frozen_manifest import (
    MANIFEST_SCHEMA_VERSION,
    UnmanifestedPanelWarning,
    refresh_frozen_manifest,
    verify_sibling_manifest,
)
from stock_grader.panel import build_panel, write_panel


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frozen_profile_dir(root: Path, profile: str = "all_weather") -> Path:
    for signal in SIGNALS:
        _write_frozen(root, signal, _healthy_rows(), profile=profile)
    return root / profile


def _tamper(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"tampered")


# -- writer --------------------------------------------------------------------


def test_refresh_writes_catalog_with_distinct_format_and_content_versions(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    manifest_path = refresh_frozen_manifest(directory)
    manifest = json.loads(manifest_path.read_text())
    # The manifest FORMAT version and the dataset CONTENT version are separate
    # fields: the day one bumps without the other, a shared field would make
    # the bump invisible.
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["content_schema_versions"] == ["1.0"]
    assert manifest["dataset"] == "frozen_score_panel"
    assert manifest["profile"] == "all_weather"
    entries = {entry["name"]: entry for entry in manifest["files"]}
    assert set(entries) == {f"{signal.isoformat()}.parquet" for signal in SIGNALS}
    for name, entry in entries.items():
        part = directory / name
        assert entry["sha256"] == _sha256(part)
        assert entry["bytes"] == part.stat().st_size
        assert entry["rows"] == len(pd.read_parquet(part))
        assert set(entry["columns"]) == set(pd.read_parquet(part).columns)
        assert entry["content_schema_version"] == "1.0"
        assert entry["hashed_at"] == "backfill"  # bytes predate their catalog


def test_refresh_catalogs_only_date_stem_parts(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    stray = directory / "not-a-signal-date.parquet"
    pd.DataFrame({"x": [1]}).to_parquet(stray, index=False)
    manifest = json.loads(refresh_frozen_manifest(directory).read_text())
    names = {entry["name"] for entry in manifest["files"]}
    assert stray.name not in names  # readers ignore it, so the catalog must too


def test_refresh_is_idempotent_and_carries_provenance_forward(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    first = refresh_frozen_manifest(
        directory, frozen_now=frozenset({f"{SIGNALS[0].isoformat()}.parquet"})
    )
    before = first.read_bytes()
    # Nothing changed: the manifest is not rewritten (no generated_at churn).
    assert refresh_frozen_manifest(directory).read_bytes() == before

    # A new part accrues: additive refresh. The old parts keep the provenance
    # the first catalog recorded; only the new part is described afresh.
    new_signal = dt.date(2026, 9, 1)
    _write_frozen(tmp_path / "frozen", new_signal, _healthy_rows())
    manifest = json.loads(
        refresh_frozen_manifest(
            directory, frozen_now=frozenset({f"{new_signal.isoformat()}.parquet"})
        ).read_text()
    )
    provenance = {entry["name"]: entry["hashed_at"] for entry in manifest["files"]}
    assert provenance[f"{SIGNALS[0].isoformat()}.parquet"] == "freeze"
    assert provenance[f"{SIGNALS[1].isoformat()}.parquet"] == "backfill"
    assert provenance[f"{new_signal.isoformat()}.parquet"] == "freeze"


def test_refresh_refuses_a_part_with_mixed_row_schema_versions(tmp_path):
    directory = tmp_path / "frozen" / "all_weather"
    directory.mkdir(parents=True)
    rows = _healthy_rows()
    rows[0]["schema_version"] = "2.0"
    frame = pd.DataFrame([{"signal_date": "2026-08-03", **row} for row in rows])
    frame.to_parquet(directory / "2026-08-03.parquet", index=False)
    with pytest.raises(ValueError, match="mixes row schema_version"):
        refresh_frozen_manifest(directory)


# -- verifier ------------------------------------------------------------------


def test_verify_passes_a_cataloged_untampered_part(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    refresh_frozen_manifest(directory)
    assert verify_sibling_manifest(directory / f"{SIGNALS[0].isoformat()}.parquet") is True


def test_verify_warns_and_reads_when_no_manifest_exists(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    with pytest.warns(UnmanifestedPanelWarning, match="no sibling manifest.json"):
        verified = verify_sibling_manifest(directory / f"{SIGNALS[0].isoformat()}.parquet")
    assert verified is False


def test_verify_refuses_tampered_bytes(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    refresh_frozen_manifest(directory)
    part = directory / f"{SIGNALS[0].isoformat()}.parquet"
    _tamper(part)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        verify_sibling_manifest(part)


def test_verify_refuses_a_part_the_catalog_does_not_know(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    refresh_frozen_manifest(directory)
    unlisted = directory / "2027-01-01.parquet"
    pd.DataFrame({"x": [1]}).to_parquet(unlisted, index=False)
    with pytest.raises(ValueError, match="not cataloged"):
        verify_sibling_manifest(unlisted)


def test_verify_refuses_unknown_manifest_format_and_corrupt_manifest(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    part = directory / f"{SIGNALS[0].isoformat()}.parquet"
    manifest_path = refresh_frozen_manifest(directory)

    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "9.9"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unknown manifest schema_version"):
        verify_sibling_manifest(part)

    manifest_path.write_text("{not json")
    with pytest.raises(ValueError, match="cannot be parsed"):
        verify_sibling_manifest(part)


# -- freeze writes the catalog -------------------------------------------------


def _patched_freeze(tmp_path, monkeypatch):
    snapshots = _universe(16, with_prices=False)
    _patch_freeze_universe(
        monkeypatch,
        snapshots,
        universe_id="liq1000_v1",
        asof=dt.date(2026, 7, 29),
        spec_sha256="a" * 64,
        source_sha256="b" * 64,
    )
    monkeypatch.setattr(cli, "_build_snapshots", lambda tickers, args, provider: snapshots)
    return _freeze_args(tmp_path / "frozen", profile="all_weather")


def test_freeze_writes_manifest_beside_the_panel(tmp_path, monkeypatch):
    args = _patched_freeze(tmp_path, monkeypatch)
    assert cli.cmd_freeze(args) == 0
    directory = tmp_path / "frozen" / "all_weather"
    manifest = json.loads((directory / "manifest.json").read_text())
    (entry,) = manifest["files"]
    assert entry["name"] == "2026-07-29.parquet"
    assert entry["sha256"] == _sha256(directory / "2026-07-29.parquet")
    assert entry["hashed_at"] == "freeze"  # cataloged by the run that froze it
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["content_schema_versions"] == ["1.0"]
    # The frozen part now verifies at every consuming boundary.
    assert verify_sibling_manifest(directory / "2026-07-29.parquet") is True

    # Second run freezes nothing and must not churn the catalog either.
    before = (directory / "manifest.json").read_bytes()
    assert cli.cmd_freeze(args) == 0
    assert (directory / "manifest.json").read_bytes() == before


def test_freeze_backfills_a_preexisting_unmanifested_directory(tmp_path, monkeypatch):
    # Parts frozen before the manifest convention existed: the next freeze
    # catalogs them additively (hashed after the fact -> "backfill") without
    # rewriting a single immutable part.
    _write_frozen(tmp_path / "frozen", dt.date(2026, 7, 1), _healthy_rows())
    old = (tmp_path / "frozen" / "all_weather" / "2026-07-01.parquet").read_bytes()
    args = _patched_freeze(tmp_path, monkeypatch)
    assert cli.cmd_freeze(args) == 0
    directory = tmp_path / "frozen" / "all_weather"
    assert (directory / "2026-07-01.parquet").read_bytes() == old
    provenance = {
        entry["name"]: entry["hashed_at"]
        for entry in json.loads((directory / "manifest.json").read_text())["files"]
    }
    assert provenance == {
        "2026-07-01.parquet": "backfill",
        "2026-07-29.parquet": "freeze",
    }


def test_freeze_goes_red_on_a_part_it_cannot_catalog(tmp_path, monkeypatch, capsys):
    """Corrupt bytes in the frozen tree must never be blessed OR silently
    skipped: the part stays uncataloged (downstream consumers refuse it) and
    the freeze run itself goes red."""
    corrupt = tmp_path / "frozen" / "all_weather" / "2026-07-01.parquet"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not parquet")
    args = _patched_freeze(tmp_path, monkeypatch)
    assert cli.cmd_freeze(args) == 2
    assert "manifest not written" in capsys.readouterr().out
    # The new panel was still frozen; only the catalog is missing.
    assert (tmp_path / "frozen" / "all_weather" / "2026-07-29.parquet").exists()
    assert not (tmp_path / "frozen" / "all_weather" / "manifest.json").exists()


# -- consuming boundaries refuse tampered parts --------------------------------


def test_backtest_cli_refuses_a_tampered_panel(tmp_path, capsys):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    refresh_frozen_manifest(directory)
    part = directory / f"{SIGNALS[0].isoformat()}.parquet"
    _tamper(part)
    # 2, the documented refusal code — a refusal RETURNS rather than falling
    # through to main()'s generic exception handler (exit 1).
    assert cli.main(["backtest", str(part)]) == 2
    assert "sha256 mismatch" in capsys.readouterr().out


def test_panel_loader_warns_on_unmanifested_panel_and_still_loads(tmp_path):
    """The non-strict path — genuinely pre-convention directories stay readable."""
    directory = _frozen_profile_dir(tmp_path / "frozen")
    part = directory / f"{SIGNALS[0].isoformat()}.parquet"
    with pytest.warns(UnmanifestedPanelWarning):
        frame = cli._load_panel_frame(part, strict=False)
    assert len(frame) == len(_healthy_rows())


def test_build_panel_refuses_a_tampered_frozen_part(tmp_path):
    root = tmp_path / "frozen"
    directory = _frozen_profile_dir(root)
    refresh_frozen_manifest(directory)
    _tamper(directory / f"{SIGNALS[0].isoformat()}.parquet")
    vault = VaultDataSource(_build_market_vault(tmp_path / "vault"))
    result = build_panel(root, "all_weather", vault, _Foundry(), _config(), today=TODAY)
    assert result.panel is None
    assert result.refusal is not None and "sha256 mismatch" in result.refusal


def test_build_panel_verifies_and_builds_from_a_manifested_directory(tmp_path):
    root = tmp_path / "frozen"
    refresh_frozen_manifest(_frozen_profile_dir(root))
    vault = VaultDataSource(_build_market_vault(tmp_path / "vault"))
    result = build_panel(root, "all_weather", vault, _Foundry(), _config(), today=TODAY)
    assert result.refusal is None
    assert result.panel is not None

    # write_panel catalogs its own output, so the backtest consumption of the
    # built panel verifies instead of trusting a bare path.
    out_dir = tmp_path / "build"
    panel_path, sidecar_path = write_panel(result, out_dir, "all_weather", _config())
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["dataset"] == "built_forward_panel"
    entries = {entry["name"]: entry for entry in manifest["files"]}
    assert set(entries) == {panel_path.name, sidecar_path.name}
    assert all(entry["hashed_at"] == "build" for entry in entries.values())
    assert verify_sibling_manifest(panel_path) is True


def test_shipped_frozen_directories_are_fully_cataloged():
    """The committed evidence tree must satisfy its own convention: every
    shipped frozen part verifies against the sibling manifest, and no shipped
    directory is left unmanifested. This is what makes the backfill — and
    every future freeze commit — checkable rather than trusted."""
    repo = Path(__file__).resolve().parent.parent
    for root_name in ("frozen_scores", "frozen_scores_wide"):
        root = repo / root_name
        if not root.is_dir():  # wheel installs ship no evidence tree
            continue
        for profile_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            assert (profile_dir / "manifest.json").is_file(), profile_dir
            for part in sorted(profile_dir.glob("*.parquet")):
                assert verify_sibling_manifest(part) is True, part


def test_decay_loader_refuses_a_tampered_part_and_warns_without_manifest(tmp_path):
    directory = _frozen_profile_dir(tmp_path / "frozen")
    with pytest.warns(UnmanifestedPanelWarning):
        frame = load_frozen_panels(directory)
    assert not frame.empty

    refresh_frozen_manifest(directory)
    _tamper(directory / f"{SIGNALS[1].isoformat()}.parquet")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        load_frozen_panels(directory)
