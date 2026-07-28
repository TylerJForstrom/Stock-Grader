"""Deterministic, auditable comparable-company selection.

The grading engine is cross-sectional, so the peer set is part of the model rather than a
presentation detail.  This module deliberately uses only fields already present in
``SecuritySnapshot``: SIC, business-model class, reporting currency, and market capitalisation.
It does not pretend that a broad large-cap convenience universe is an industry comp set.

Automatic selection is conservative:

* a bank, insurer, REIT, utility, or energy company is never widened into a different
  business-model class;
* four-digit SIC is preferred, then three- and two-digit SIC;
* similarly-sized companies are preferred when both market caps are available;
* every widening step, exclusion, and selected ticker is recorded in a fingerprinted manifest.

For historical work the caller remains responsible for supplying an as-of membership universe.
Selecting peers well cannot repair survivor bias in the candidate list, so the manifest records
that candidate-universe provenance explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field

from .types import SecuritySnapshot

__all__ = ["PeerSelection", "explicit_peers", "select_peers"]


@dataclass(slots=True)
class PeerSelection:
    """Machine-readable record of how an automatic comp set was constructed."""

    target: str
    asof: str
    rule: str
    candidate_universe: str
    members: list[str] = field(default_factory=list)
    member_identifiers: list[str] = field(default_factory=list)
    widening_steps: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _sic_digits(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:4]


def _size_distance(target: SecuritySnapshot, candidate: SecuritySnapshot) -> float:
    """Absolute log-size distance; unknown size sorts behind known comparable sizes."""

    left, right = target.market_cap, candidate.market_cap
    if left is None or right is None or left <= 0 or right <= 0:
        return float("inf")
    return abs(math.log(right / left))


def _within_size_band(
    target: SecuritySnapshot,
    candidate: SecuritySnapshot,
    multiple: float,
) -> bool:
    left, right = target.market_cap, candidate.market_cap
    if left is None or right is None or left <= 0 or right <= 0:
        return False
    ratio = right / left
    return 1.0 / multiple <= ratio <= multiple


def _fingerprint(selection: PeerSelection) -> str:
    payload = {
        "target": selection.target,
        "asof": selection.asof,
        "rule": selection.rule,
        "candidate_universe": selection.candidate_universe,
        "members": selection.members,
        "member_identifiers": selection.member_identifiers,
        "widening_steps": selection.widening_steps,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _security_key(snapshot: SecuritySnapshot) -> str:
    if snapshot.cik:
        raw = str(snapshot.cik).strip()
        return f"CIK:{raw.zfill(10) if raw.isdigit() else raw.upper()}"
    return f"TICKER:{snapshot.ticker.upper()}"


def explicit_peers(
    target: SecuritySnapshot,
    candidates: list[SecuritySnapshot],
    *,
    candidate_universe: str = "explicit",
) -> tuple[list[SecuritySnapshot], PeerSelection]:
    """Record a caller-authored peer set without silently filtering its investment thesis."""

    peers: list[SecuritySnapshot] = []
    seen_tickers: set[str] = set()
    seen_identifiers: set[str] = set()
    excluded: dict[str, str] = {}
    target_identifier = _security_key(target)
    for candidate in candidates:
        ticker = candidate.ticker.upper()
        identifier = _security_key(candidate)
        if ticker == target.ticker.upper() or identifier == target_identifier:
            excluded[ticker] = "target security"
        elif ticker in seen_tickers or identifier in seen_identifiers:
            excluded[ticker] = "duplicate security identifier or ticker"
        else:
            peers.append(candidate)
            seen_tickers.add(ticker)
            seen_identifiers.add(identifier)
    manifest = PeerSelection(
        target=target.ticker.upper(),
        asof=target.asof.isoformat(),
        rule="explicit caller-authored peer set; no automatic exclusions",
        candidate_universe=candidate_universe,
        members=[candidate.ticker.upper() for candidate in peers],
        member_identifiers=[_security_key(candidate) for candidate in peers],
        widening_steps=["explicit membership retained"],
        excluded=excluded,
    )
    if len(peers) < 2:
        manifest.warnings.append(
            "fewer than two explicit peers were supplied; cross-sectional grading is not reliable"
        )
    business_models = sorted({candidate.sector.value for candidate in peers})
    if any(candidate.sector is not target.sector for candidate in peers):
        manifest.warnings.append(
            "explicit peer set crosses business-model classes: "
            + ", ".join(business_models)
        )
    mismatched_dates = sorted(
        candidate.ticker.upper()
        for candidate in peers
        if candidate.asof != target.asof
    )
    if mismatched_dates:
        manifest.warnings.append(
            "explicit peers use different as-of dates: " + ", ".join(mismatched_dates)
        )
    manifest.fingerprint = _fingerprint(manifest)
    return peers, manifest


def select_peers(
    target: SecuritySnapshot,
    candidates: list[SecuritySnapshot],
    *,
    minimum: int = 8,
    maximum: int = 30,
    size_band_multiple: float = 5.0,
    candidate_universe: str = "caller_supplied",
) -> tuple[list[SecuritySnapshot], PeerSelection]:
    """Select a defensible comp set from already-loaded candidate snapshots.

    Selection is deterministic and independent of input order.  It first accumulates candidates
    inside a market-cap band through progressively broader SIC tiers, then repeats the same tiers
    without a size requirement if the minimum has not been reached.  It never widens across the
    target's :class:`~stock_grader.types.SectorClass`.
    """

    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 2:
        raise ValueError("minimum peer count must be at least 2")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < minimum:
        raise ValueError("maximum peer count must be greater than or equal to minimum")
    try:
        size_band_multiple = float(size_band_multiple)
    except (TypeError, ValueError) as exc:
        raise ValueError("size_band_multiple must be numeric") from exc
    if not math.isfinite(size_band_multiple) or size_band_multiple <= 1.0:
        raise ValueError("size_band_multiple must be finite and greater than 1")

    target_sic = _sic_digits(target.sic)
    rule = (
        "same business model; SIC4→SIC3→SIC2; "
        f"prefer market cap within {size_band_multiple:g}x"
    )
    manifest = PeerSelection(
        target=target.ticker.upper(),
        asof=target.asof.isoformat(),
        rule=rule,
        candidate_universe=candidate_universe,
    )

    unique: dict[str, SecuritySnapshot] = {}
    seen_tickers: set[str] = set()
    target_identifier = _security_key(target)
    for candidate in candidates:
        ticker = candidate.ticker.upper()
        identifier = _security_key(candidate)
        if ticker == target.ticker.upper() or identifier == target_identifier:
            manifest.excluded[ticker] = "target security"
            continue
        if ticker in seen_tickers or identifier in unique:
            manifest.excluded[ticker] = "duplicate security identifier or ticker"
            continue
        if candidate.asof != target.asof:
            manifest.excluded[ticker] = (
                f"as-of date {candidate.asof.isoformat()} differs from "
                f"{target.asof.isoformat()}"
            )
            continue
        if candidate.fundamentals is None:
            manifest.excluded[ticker] = "no usable fundamentals"
            continue
        if candidate.currency != target.currency:
            manifest.excluded[ticker] = (
                f"reporting currency {candidate.currency} differs from {target.currency}"
            )
            continue
        if candidate.sector is not target.sector:
            manifest.excluded[ticker] = (
                f"business model {candidate.sector.value} differs from {target.sector.value}"
            )
            continue
        unique[identifier] = candidate
        seen_tickers.add(ticker)

    pool = sorted(
        unique.values(),
        key=lambda item: (_size_distance(target, item), item.ticker.upper()),
    )
    selected: list[SecuritySnapshot] = []
    selected_tickers: set[str] = set()

    def sic_matches(candidate: SecuritySnapshot, digits: int) -> bool:
        candidate_sic = _sic_digits(candidate.sic)
        return bool(
            target_sic
            and candidate_sic
            and len(target_sic) >= digits
            and len(candidate_sic) >= digits
            and target_sic[:digits] == candidate_sic[:digits]
        )

    passes: list[tuple[str, int | None, bool]] = [
        ("same 4-digit SIC and size band", 4, True),
        ("widened to 3-digit SIC and size band", 3, True),
        ("widened to 2-digit SIC and size band", 2, True),
        ("widened to business-model class and size band", None, True),
        ("relaxed size band within 4-digit SIC", 4, False),
        ("relaxed size band within 3-digit SIC", 3, False),
        ("relaxed size band within 2-digit SIC", 2, False),
        ("relaxed size band within business-model class", None, False),
    ]

    for label, digits, require_size in passes:
        before = len(selected)
        for candidate in pool:
            ticker = candidate.ticker.upper()
            if ticker in selected_tickers:
                continue
            if digits is not None and not sic_matches(candidate, digits):
                continue
            if require_size and not _within_size_band(target, candidate, size_band_multiple):
                continue
            selected.append(candidate)
            selected_tickers.add(ticker)
            if len(selected) >= maximum:
                break
        if len(selected) > before:
            manifest.widening_steps.append(f"{label}: +{len(selected) - before}")
        if len(selected) >= minimum:
            break

    # If the first successful tier contains more than `maximum`, ordering by size distance makes
    # the retained set deterministic and economically preferable.
    selected = selected[:maximum]
    selected_tickers = {item.ticker.upper() for item in selected}
    for candidate in unique.values():
        ticker = candidate.ticker.upper()
        if ticker not in selected_tickers and ticker not in manifest.excluded:
            manifest.excluded[ticker] = "outside selected SIC/size priority or maximum peer count"

    manifest.members = [item.ticker.upper() for item in selected]
    manifest.member_identifiers = [_security_key(item) for item in selected]
    if len(selected) < minimum:
        manifest.warnings.append(
            f"only {len(selected)} defensible peers were available; requested at least {minimum}"
        )
    if target.market_cap is None:
        manifest.warnings.append(
            "target market capitalisation unavailable; peer size could not be matched"
        )
    if not target_sic:
        manifest.warnings.append(
            "target SIC unavailable; selection used business-model class only"
        )
    manifest.fingerprint = _fingerprint(manifest)
    return selected, manifest
