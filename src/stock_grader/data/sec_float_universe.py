"""Public-domain wide-universe selection by SEC ``dei:EntityPublicFloat``.

This is the licensing-safe fallback for M5. It reads only manifest-verified Stock-Data symbol
directories and the SEC bulk Companyfacts archive. Public output contains alphabetically sorted
membership only; public-float values and rank order are never written to the universe file.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .symbols import ticker_variants

__all__ = [
    "FloatCandidate",
    "build_sec_float_universe",
    "emit_sec_float_universes",
    "render_public_universe",
    "universe_source_sha256",
]


class _SymbolSource(Protocol):
    def universe(
        self, *, listed_only: bool = True, asof: str | None = None
    ) -> list[dict[str, Any]]: ...

    def symbol_directory(self, name: str) -> list[dict[str, Any]]: ...

    def manifest(self, dataset_dir: str) -> dict[str, Any]: ...


class _FactsSource(Protocol):
    def company_facts(self, cik: str) -> dict[str, Any] | None: ...

    def provenance(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class FloatCandidate:
    ticker: str
    cik: str
    public_float: float
    observation_end: date
    filed: date


_ISSUER_TICKER_RULE = "one_per_cik_sec_dash_ticker_ascending"
_SOURCE_MANIFESTS = ("data/symbols/current", "data/symbols/events")


def _canonical_ticker(value: object) -> str:
    ticker = str(value or "").strip().upper().replace(" ", "-")
    variants = ticker_variants(ticker)
    return variants[1] if "." in ticker and len(variants) > 1 else ticker


def _required_date(record: dict[str, Any], field: str) -> date:
    raw = record.get(field)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"EntityPublicFloat record is missing {field}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"EntityPublicFloat {field} is not an ISO date: {raw!r}") from exc


def _required_float(record: dict[str, Any]) -> float:
    if "val" not in record:
        raise ValueError("EntityPublicFloat record is missing val")
    raw = record["val"]
    if isinstance(raw, bool):
        raise ValueError("EntityPublicFloat val is not numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("EntityPublicFloat val is not numeric") from exc
    if not math.isfinite(value):
        raise ValueError("EntityPublicFloat val is not finite")
    return value


def _latest_float(facts: dict[str, Any], asof: date) -> tuple[float, date, date] | None:
    records = (
        facts.get("facts", {})
        .get("dei", {})
        .get("EntityPublicFloat", {})
        .get("units", {})
        .get("USD", [])
    )
    if not isinstance(records, list):
        raise ValueError("EntityPublicFloat USD units must be a list")
    eligible: list[tuple[date, date, float]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("EntityPublicFloat observation must be an object")
        end = _required_date(record, "end")
        filed = _required_date(record, "filed")
        value = _required_float(record)
        if end <= asof and filed <= asof and value > 0:
            eligible.append((end, filed, value))
    if not eligible:
        return None
    end, filed, value = max(eligible, key=lambda item: (item[0], item[1]))
    return value, end, filed


def _listing_flags(source: _SymbolSource) -> dict[str, tuple[bool, bool]]:
    flags: dict[str, tuple[bool, bool]] = {}
    for name in ("nasdaqlisted.jsonl", "otherlisted.jsonl"):
        for record in source.symbol_directory(name):
            ticker = _canonical_ticker(record.get("ticker"))
            if not ticker:
                continue
            etf = str(record.get("etf", "")).strip().upper() == "Y"
            test_issue = str(record.get("test_issue", "")).strip().upper() == "Y"
            previous = flags.get(ticker, (False, False))
            flags[ticker] = (previous[0] or etf, previous[1] or test_issue)
    return flags


def build_sec_float_universe(
    source: _SymbolSource,
    facts_source: _FactsSource,
    spec: dict[str, Any],
    asof: date,
) -> list[FloatCandidate]:
    """Return candidates ranked by latest PIT public float, then ticker ascending."""
    if spec.get("schema_version") != "1.0":
        raise ValueError("universe spec schema_version must be '1.0'")
    if spec.get("rank_key") != "dei:EntityPublicFloat":
        raise ValueError("SEC-float generator requires rank_key='dei:EntityPublicFloat'")
    exchanges = spec.get("listed_exchanges")
    if not isinstance(exchanges, list) or not all(isinstance(item, str) for item in exchanges):
        raise ValueError("universe spec listed_exchanges must be a list of strings")
    for key in ("exclude_etf", "exclude_test_issue", "require_sec_cik"):
        if not isinstance(spec.get(key), bool):
            raise ValueError(f"universe spec {key} must be boolean")
    if spec.get("issuer_ticker_rule") != _ISSUER_TICKER_RULE:
        raise ValueError(f"universe spec issuer_ticker_rule must be {_ISSUER_TICKER_RULE!r}")
    exclude_etf = spec["exclude_etf"]
    exclude_test_issue = spec["exclude_test_issue"]
    require_sec_cik = spec["require_sec_cik"]
    listed = set(exchanges)
    flags = _listing_flags(source)
    candidates: list[FloatCandidate] = []
    seen: set[str] = set()
    seen_ciks: set[str] = set()
    # EntityPublicFloat is issuer-level evidence. Sort first so multiple eligible securities for
    # one CIK deterministically retain the alphabetically first SEC-dash ticker.
    records = sorted(
        source.universe(listed_only=False, asof=asof.isoformat()),
        key=lambda record: _canonical_ticker(record.get("ticker")),
    )
    for record in records:
        ticker = _canonical_ticker(record.get("ticker"))
        if not ticker or ticker in seen or record.get("exchange") not in listed:
            continue
        seen.add(ticker)
        listing_flags = flags.get(ticker)
        if listing_flags is None:
            if exclude_etf or exclude_test_issue:
                continue
            listing_flags = (False, False)
        etf, test_issue = listing_flags
        if exclude_etf and etf:
            continue
        if exclude_test_issue and test_issue:
            continue
        cik_value = record.get("cik")
        if cik_value is None:
            if require_sec_cik:
                continue
            raise ValueError(f"{ticker} has no SEC CIK")
        if isinstance(cik_value, bool):
            raise ValueError(f"{ticker} has an invalid SEC CIK")
        if isinstance(cik_value, int):
            cik_number = cik_value
        elif isinstance(cik_value, str) and cik_value.strip().isdecimal():
            cik_number = int(cik_value.strip())
        else:
            raise ValueError(f"{ticker} has an invalid SEC CIK")
        if cik_number <= 0 or cik_number > 9_999_999_999:
            raise ValueError(f"{ticker} has an invalid SEC CIK")
        cik = str(cik_number).zfill(10)
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        facts = facts_source.company_facts(cik)
        if facts is None:
            continue
        observation = _latest_float(facts, asof)
        if observation is None:
            continue
        value, observation_end, filed = observation
        candidates.append(FloatCandidate(ticker, cik, value, observation_end, filed))
    return sorted(candidates, key=lambda item: (-item.public_float, item.ticker))


def render_public_universe(
    candidates: Sequence[FloatCandidate],
    *,
    universe_id: str,
    asof: date,
    spec_sha256: str,
    source_sha256: str,
) -> str:
    """Render an existing-loader-compatible membership file with no values or rank order."""
    tickers = sorted(candidate.ticker for candidate in candidates)
    header = [
        f"# universe_id: {universe_id}",
        f"# asof: {asof.isoformat()}",
        f"# spec_sha256: {spec_sha256}",
        f"# source_sha256: {source_sha256}",
        f"# row_count: {len(tickers)}",
    ]
    return "\n".join([*header, *tickers, ""])


def _immutable_write(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists():
        if path.read_bytes() == encoded:
            return
        raise FileExistsError(f"immutable universe artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tmp", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def universe_source_sha256(source: _SymbolSource, facts_source: _FactsSource) -> str:
    """Fingerprint the exact bulk archive and parsed Foundry manifests used for membership.

    Canonical JSON makes the digest independent of manifest whitespace while retaining every
    schema, source, license, file hash, byte count, and generator metadata field.
    """
    provenance = facts_source.provenance()
    if provenance is None:
        raise ValueError("SEC bulk provenance sidecar is unavailable")
    bulk_sha256 = str(provenance.get("sha256", ""))
    if re.fullmatch(r"[0-9a-fA-F]{64}", bulk_sha256) is None:
        raise ValueError("SEC bulk provenance sha256 is missing or invalid")
    manifests: dict[str, dict[str, Any]] = {}
    for dataset_dir in _SOURCE_MANIFESTS:
        manifest = source.manifest(dataset_dir)
        if not isinstance(manifest, dict):
            raise ValueError(f"Stock-Data manifest {dataset_dir} is not an object")
        manifests[dataset_dir] = manifest
    descriptor = {
        "sec_companyfacts_sha256": bulk_sha256.lower(),
        "stock_data_manifests": manifests,
    }
    canonical = json.dumps(
        descriptor, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def emit_sec_float_universes(
    source: _SymbolSource,
    facts_source: _FactsSource,
    spec_path: str | Path,
    asof: date,
    out_dir: str | Path,
    *,
    sizes: Sequence[int] = (250, 500, 1000),
) -> dict[int, Path]:
    """Build and immutably emit alphabetic stage files for N=250/500/1000."""
    spec_bytes = Path(spec_path).read_bytes()
    spec = json.loads(spec_bytes)
    ranked = build_sec_float_universe(source, facts_source, spec, asof)
    source_sha256 = universe_source_sha256(source, facts_source)
    spec_sha256 = hashlib.sha256(spec_bytes).hexdigest()
    universe_id = str(spec.get("universe_id", "")).strip()
    if not universe_id:
        raise ValueError("universe spec universe_id is missing")
    outputs: dict[int, Path] = {}
    target_size = spec.get("target_size")
    if isinstance(target_size, bool) or not isinstance(target_size, int) or target_size <= 0:
        raise ValueError("universe spec target_size must be a positive integer")
    for size in sorted(set(sizes)):
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError("universe stage sizes must be positive")
        if len(ranked) < size:
            raise ValueError(f"only {len(ranked)} eligible SEC-float names; cannot emit N={size}")
        path = Path(out_dir) / f"universe_liq{size}_{asof.isoformat()}.txt"
        stage_universe_id = universe_id if size == target_size else f"{universe_id}_stage{size}"
        content = render_public_universe(
            ranked[:size],
            universe_id=stage_universe_id,
            asof=asof,
            spec_sha256=spec_sha256,
            source_sha256=source_sha256,
        )
        _immutable_write(path, content)
        outputs[size] = path
    return outputs
