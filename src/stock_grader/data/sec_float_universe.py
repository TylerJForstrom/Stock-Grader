"""Public-domain wide-universe selection by SEC ``dei:EntityPublicFloat``.

This is the licensing-safe fallback for M5. It reads only manifest-verified Stock-Data symbol
directories and the SEC bulk Companyfacts archive. Public output contains alphabetically sorted
membership only; public-float values and rank order are never written to the universe file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from .symbols import ticker_variants

log = logging.getLogger(__name__)

__all__ = [
    "FloatCandidate",
    "UniverseBuild",
    "UniverseDrop",
    "assert_archive_covers_asof",
    "build_sec_float_universe",
    "build_sec_float_universe_with_drops",
    "emit_sec_float_universes",
    "normalized_entity_name",
    "render_drop_manifest",
    "render_public_universe",
    "universe_source_sha256",
]


class _SymbolSource(Protocol):
    def universe(
        self, *, listed_only: bool = True, asof: str | None = None
    ) -> list[dict[str, Any]]: ...

    def symbol_directory(self, name: str, *, asof: str | None = None) -> list[dict[str, Any]]: ...

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
    # Set when the directory CIK carried no usable rank observation and the
    # issuer's own filing CIK supplied it instead. None means no substitution.
    resolved_from_cik: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseDrop:
    """One candidate the selection rule declined to seat, and why.

    Every exclusion that is not a plain rule outcome lands here. A silently
    dropped primary listing is indistinguishable from a name that was never
    eligible, and that is how XOM vanished from a thousand-name universe without
    anyone noticing.
    """

    ticker: str
    cik: str | None
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UniverseBuild:
    candidates: list[FloatCandidate]
    #: Candidates the rule EXCLUDED. Disjoint from ``candidates`` by construction,
    #: so a consumer can reconstruct the eligible pool by complement.
    drops: list[UniverseDrop]
    #: Things worth recording about candidates that WERE seated — a rejected
    #: observation, a substituted filer CIK. These are not exclusions and must
    #: never be mixed into ``drops``, or the universe and its manifest contradict
    #: each other about whether a name is a member.
    notes: list[UniverseDrop]


@dataclass(frozen=True, slots=True)
class _ListingRow:
    etf: bool
    test_issue: bool
    security_name: str


_ISSUER_TICKER_RULE = "one_per_cik_sec_dash_ticker_ascending"
_ISSUER_TICKER_RULE_COMMON_FIRST = "one_per_cik_common_equity_then_sec_dash_ticker_ascending"
_ISSUER_TICKER_RULES = frozenset({_ISSUER_TICKER_RULE, _ISSUER_TICKER_RULE_COMMON_FIRST})
_SOURCE_MANIFESTS = ("data/symbols/current", "data/symbols/events")

# Plausibility ceiling for dei:EntityPublicFloat, in USD.
#
# Public float cannot exceed market capitalisation. The largest US market cap
# observed through 2026 is roughly $5T (NVDA's own filed float is $4.0T), so a
# $10T ceiling sits about 2.5x above any real filer and still rejects the
# scale-error filings that XBRL cover-page tagging produces — Cabot Corp filed
# $4.43 QUADRILLION and ranked first in the committed N=1000 universe.
_DEFAULT_MAX_PUBLIC_FLOAT_USD = 1e13

# Recency ceiling for the rank observation, measured on the FILED date, in days
# before asof.
#
# It must be `filed`, not the cover-page `end`. `end` is a value the filer writes,
# and real filers repeat a stale one: AMD's 10-K filed 2026-02-04 still carries
# end=2024-06-28, so an `end`-based window drops a $232 billion constituent and
# labels it dormant. `filed` is the fact that matters — the date the issuer last
# told SEC this number. Two years tolerates a late filer plus one missed annual
# cycle while still rejecting issuers whose only float tag was filed in 2010.
_DEFAULT_MAX_OBSERVATION_AGE_DAYS = 730

# How far after asof the bulk archive may have been generated. One week absorbs a
# weekend plus a holiday between the as-of date and the build without letting the
# artifact describe a materially later state of EDGAR.
_ARCHIVE_MAX_LAG_DAYS = 7

# Nasdaq Trader security descriptions for equity that represents ownership of the
# issuer. Everything else under the same CIK — ZONES, notes, preferred series,
# warrants, units, rights — is a different instrument that must not be seated as
# the issuer's listing. "Comcast Holdings ZONES" (CCZ) outranked "Comcast
# Corporation - Class A Common Stock" (CMCSA) purely on alphabetical order.
_COMMON_EQUITY_NAME = re.compile(
    r"\b(COMMON\s+STOCK|COMMON\s+SHARES|ORDINARY\s+SHARES|CLASS\s+[A-Z]\s+COMMON|"
    r"AMERICAN\s+DEPOSITARY\s+(?:SHARES|RECEIPTS))\b",
    re.IGNORECASE,
)

_MIN_ENTITY_NAME_KEY = 6
_ENTITY_NAME_SUFFIX = re.compile(
    r"[\s,.]+(CORPORATION|CORP|INCORPORATED|INC|COMPANY|LIMITED|LTD|PLC|LLC|LP|NV|SA|AG|CO)$"
)


def normalized_entity_name(name: object) -> str:
    """Collapse a registrant name to a comparison key.

    SEC writes the same issuer differently in different files, so punctuation,
    spacing, case, and a trailing corporate suffix are removed. Nothing else is
    stripped: dropping words like HOLDINGS or GROUP merges genuinely different
    registrants.
    """
    text = str(name or "").upper().replace("\xa0", " ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):
        shortened = _ENTITY_NAME_SUFFIX.sub("", text).strip()
        if shortened == text:
            break
        text = shortened
    return text.replace(" ", "")


def _cik_text(value: object) -> str | None:
    """Render a CIK as the canonical zero-padded string, or None when absent.

    The drop manifest is a published artifact; a field that is sometimes an int
    and sometimes a padded string cannot be joined or sorted by a consumer.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value).zfill(10)
    text = str(value).strip()
    if not text:
        return None
    return text.zfill(10) if text.isdecimal() else text


def _spec_positive_number(spec: dict[str, Any], key: str, default: float) -> float:
    """Read an optional positive numeric bound from the spec, refusing junk.

    Absent means "use the documented default"; present-but-unparseable is a
    malformed specification and must raise rather than silently fall back.
    """
    if key not in spec:
        return default
    raw = spec[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"universe spec {key} must be a positive number")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"universe spec {key} must be a positive number")
    return value


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


def _has_dei_taxonomy(facts: dict[str, Any]) -> bool:
    """True when the member reports under the ``dei`` taxonomy at all.

    A registrant that has never filed a periodic report — a freshly created
    holding-company shell, for instance — carries only registration taxonomies
    such as ``ffd``. That is the signature separating "this CIK is the wrong one
    for this issuer" from "this issuer legitimately never reports a public
    float": closed-end funds and N-CSR filers do have ``dei``, and must not be
    name-matched onto some other registrant.
    """
    dei = facts.get("facts", {}).get("dei")
    return isinstance(dei, dict) and bool(dei)


def _latest_float(
    facts: dict[str, Any],
    asof: date,
    *,
    max_value: float,
    max_age_days: float,
    rejections: list[tuple[str, str]] | None = None,
) -> tuple[float, date, date] | None:
    """Latest point-in-time public float, subject to plausibility and recency bounds.

    ``rejections`` collects ``(reason, detail)`` for every observation the bounds
    threw out, so a discarded value is auditable rather than invisible. A bound
    breach rejects that one observation and lets an older, plausible one stand;
    it never raises. One malformed cover-page tag must not be able to kill a
    thousand-name universe build — but it must never pass silently either, which
    is why every rejection is returned to the caller and lands in the drop
    manifest and the log.
    """
    records = (
        facts.get("facts", {})
        .get("dei", {})
        .get("EntityPublicFloat", {})
        .get("units", {})
        .get("USD", [])
    )
    if not isinstance(records, list):
        raise ValueError("EntityPublicFloat USD units must be a list")
    oldest_filed = asof - timedelta(days=max_age_days)
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
    # Walk newest-first and bound only the observation that would actually be
    # selected. A superseded historical value is ordinary filing history, not a
    # defect, so it must not be reported; the value that WOULD have set this
    # issuer's rank is the one worth objecting to.
    for end, filed, value in sorted(eligible, key=lambda item: (item[1], item[0]), reverse=True):
        if filed < oldest_filed:
            # Sorted by filed descending, so everything after this is staler too.
            if rejections is not None:
                rejections.append(
                    (
                        "float_observation_too_stale",
                        (
                            f"end={end.isoformat()} filed={filed.isoformat()} "
                            f"oldest_filed={oldest_filed.isoformat()}"
                        ),
                    )
                )
            return None
        if value > max_value:
            if rejections is not None:
                rejections.append(
                    (
                        "float_exceeds_plausibility_ceiling",
                        (
                            f"end={end.isoformat()} filed={filed.isoformat()} "
                            f"val={value:.6g} ceiling={max_value:.6g}"
                        ),
                    )
                )
            continue
        return value, end, filed
    return None



def _listing_flags(source: _SymbolSource, asof: date) -> dict[str, _ListingRow]:
    """Read the exchange listing directories AS OF the selection date.

    Passing ``asof`` is load-bearing, not cosmetic. The directories under
    ``data/symbols/current`` describe TODAY. Screening a dated universe through
    them silently deletes every issuer delisted between ``asof`` and today, which
    is exactly the survivorship bias this selection rule exists to eliminate. The
    foundry replays the directory backward from its event stream so a name listed
    on ``asof`` is still listed here.
    """
    flags: dict[str, _ListingRow] = {}
    for name in ("nasdaqlisted.jsonl", "otherlisted.jsonl"):
        for record in source.symbol_directory(name, asof=asof.isoformat()):
            ticker = _canonical_ticker(record.get("ticker"))
            if not ticker:
                continue
            etf = str(record.get("etf", "")).strip().upper() == "Y"
            test_issue = str(record.get("test_issue", "")).strip().upper() == "Y"
            security_name = str(record.get("name", "") or "").strip()
            previous = flags.get(ticker)
            if previous is None:
                flags[ticker] = _ListingRow(etf, test_issue, security_name)
            else:
                flags[ticker] = _ListingRow(
                    previous.etf or etf,
                    previous.test_issue or test_issue,
                    previous.security_name or security_name,
                )
    return flags


def _seating_sort_key(ticker: str, row: _ListingRow | None, common_first: bool):
    """Order the securities contending to represent one CIK.

    Under the legacy rule this is ticker-ascending, exactly as before. Under the
    common-equity-first rule, a security whose exchange description says it is
    common/ordinary equity outranks one that does not, so "Comcast Holdings
    ZONES" can no longer take Comcast's seat from "Comcast Corporation - Class A
    Common Stock" on the strength of spelling alone. Within each group the tie
    break is still ticker-ascending, so the rule stays deterministic.
    """
    if not common_first:
        return (0, ticker)
    name = row.security_name if row is not None else ""
    return (0 if _COMMON_EQUITY_NAME.search(name) else 1, ticker)


def _resolve_filer_cik(
    facts_source: _FactsSource,
    directory_cik: str,
    shell_facts: dict[str, Any],
    seen_ciks: set[str],
) -> tuple[str, dict[str, Any], str] | None:
    """Find the CIK that actually files for an issuer whose directory CIK is a shell.

    SEC reassigns a ticker to a newly created holding company at reorganisation
    while the periodic reports stay under the operating company's CIK. XOM is the
    live example: ``company_tickers_exchange.json`` points at CIK 2115436, whose
    companyfacts member carries only ``ffd`` registration data, so the largest US
    energy company silently fell out of a thousand-name universe.

    The only link between the two CIKs available in public-domain data is the
    registrant name, so this is an INFERENCE, not proven succession, and the
    caller records it as such. It is deliberately narrow: it runs only for a
    member with no ``dei`` taxonomy at all, it requires the normalised name to
    select exactly one other CIK, and it refuses a CIK already seated for another
    ticker so the one-issuer-one-seat invariant cannot be broken.
    """
    index = getattr(facts_source, "entity_name_index", None)
    if not callable(index):
        return None
    # Both sides of the comparison must come from companyfacts' own entityName.
    # The ticker directory spells the same issuer differently — "ExxonMobil
    # Holdings Corp" there against "EXXON MOBIL CORP" in the archive — and those
    # normalise to different keys, so matching the directory title against an
    # archive-built index would never fire.
    key = normalized_entity_name(shell_facts.get("entityName"))
    if len(key) < _MIN_ENTITY_NAME_KEY:
        return None
    matches = [c for c in index().get(key, ()) if c != directory_cik and c not in seen_ciks]
    if len(matches) != 1:
        return None
    candidate = matches[0]
    facts = facts_source.company_facts(candidate)
    if facts is None or not _has_dei_taxonomy(facts):
        return None
    return candidate, facts, key


def build_sec_float_universe(
    source: _SymbolSource,
    facts_source: _FactsSource,
    spec: dict[str, Any],
    asof: date,
) -> list[FloatCandidate]:
    """Return candidates ranked by latest PIT public float, then ticker ascending."""
    return build_sec_float_universe_with_drops(source, facts_source, spec, asof).candidates


def build_sec_float_universe_with_drops(
    source: _SymbolSource,
    facts_source: _FactsSource,
    spec: dict[str, Any],
    asof: date,
) -> UniverseBuild:
    """Rank candidates by latest PIT public float and report every exclusion.

    Same selection as :func:`build_sec_float_universe`, but it also returns the
    drop record for each candidate that did not make it. Every ``continue`` below
    is accounted for: a universe that quietly loses its largest constituents is
    indistinguishable from one that never had them.
    """
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
    issuer_rule = spec.get("issuer_ticker_rule")
    if issuer_rule not in _ISSUER_TICKER_RULES:
        raise ValueError(
            f"universe spec issuer_ticker_rule must be one of {sorted(_ISSUER_TICKER_RULES)!r}"
        )
    common_first = issuer_rule == _ISSUER_TICKER_RULE_COMMON_FIRST
    max_float = _spec_positive_number(spec, "max_public_float_usd", _DEFAULT_MAX_PUBLIC_FLOAT_USD)
    max_age = _spec_positive_number(
        spec, "max_observation_age_days", _DEFAULT_MAX_OBSERVATION_AGE_DAYS
    )
    exclude_etf = spec["exclude_etf"]
    exclude_test_issue = spec["exclude_test_issue"]
    require_sec_cik = spec["require_sec_cik"]
    listed = set(exchanges)
    flags = _listing_flags(source, asof)
    candidates: list[FloatCandidate] = []
    drops: list[UniverseDrop] = []
    notes: list[UniverseDrop] = []
    seen: set[str] = set()
    # cik -> the ticker actually seated for that issuer. A seat is claimed only
    # when a candidate is successfully ranked, so a security that fails the float
    # check does not take the issuer's seat down with it: Aegon's real NYSE
    # listing AEG was being dropped as a "duplicate" of AEFC, a sibling that had
    # itself been rejected, leaving the issuer represented by nothing at all.
    seated_ciks: dict[str, str] = {}

    def drop(ticker: str, cik: object, reason: str, detail: str = "") -> None:
        drops.append(UniverseDrop(ticker, _cik_text(cik), reason, detail))

    def note(ticker: str, cik: object, reason: str, detail: str = "") -> None:
        notes.append(UniverseDrop(ticker, _cik_text(cik), reason, detail))

    # EntityPublicFloat is issuer-level evidence. Sort so the securities contending for one
    # CIK are considered in seating order and the winner is deterministic.
    records = sorted(
        source.universe(listed_only=False, asof=asof.isoformat()),
        key=lambda record: _seating_sort_key(
            _canonical_ticker(record.get("ticker")),
            flags.get(_canonical_ticker(record.get("ticker"))),
            common_first,
        ),
    )
    for record in records:
        ticker = _canonical_ticker(record.get("ticker"))
        if not ticker or ticker in seen:
            continue
        if record.get("exchange") not in listed:
            # Deliberately NOT marked seen: a later listed record for the same
            # canonical ticker must still be able to claim it, exactly as before.
            drop(ticker, record.get("cik"), "exchange_not_listed", str(record.get("exchange", "")))
            continue
        seen.add(ticker)
        listing_flags = flags.get(ticker)
        if listing_flags is None:
            if exclude_etf or exclude_test_issue:
                # Stock-Data drops test issues at write time, so a test issue always
                # presents as a MISSING row rather than test_issue="Y". Treating
                # missing as eligible would readmit exactly what the spec excludes,
                # so the candidate stays out — but loudly, not silently.
                drop(ticker, record.get("cik"), "listing_directory_row_missing")
                continue
            listing_flags = _ListingRow(False, False, "")
        if exclude_etf and listing_flags.etf:
            drop(ticker, record.get("cik"), "excluded_etf")
            continue
        if exclude_test_issue and listing_flags.test_issue:
            drop(ticker, record.get("cik"), "excluded_test_issue")
            continue
        cik_value = record.get("cik")
        if cik_value is None:
            if require_sec_cik:
                drop(ticker, None, "missing_sec_cik")
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
        if cik in seated_ciks:
            # Another security already represents this issuer. Name it, so a
            # displaced primary listing (BRK-B behind BRK-A) is visible rather
            # than lost.
            drop(ticker, cik, "duplicate_cik", f"retained_ticker={seated_ciks[cik]}")
            continue
        # Deliberately not memoised: the payloads are megabytes each and holding
        # a few thousand of them costs more than the occasional re-read. A CIK is
        # only read twice when its first security failed, and the archive read is
        # cheap next to keeping the whole cross-section resident.
        facts = facts_source.company_facts(cik)
        resolved_from: str | None = None
        if facts is None:
            drop(ticker, cik, "no_companyfacts_member")
            continue
        if not _has_dei_taxonomy(facts):
            resolution = _resolve_filer_cik(facts_source, cik, facts, set(seated_ciks))
            if resolution is None:
                drop(ticker, cik, "companyfacts_member_has_no_dei_taxonomy")
                continue
            filer_cik, facts, matched_key = resolution
            resolved_from, cik = cik, filer_cik
            log.warning(
                "%s: directory CIK %s reports no dei taxonomy; inferring filer CIK %s from "
                "registrant name %r (inference, not proven succession)",
                ticker,
                resolved_from,
                filer_cik,
                matched_key,
            )
            note(
                ticker,
                resolved_from,
                "filer_cik_substituted",
                f"seated_cik={filer_cik} basis=entity_name_match_unattested key={matched_key}",
            )
        rejections: list[tuple[str, str]] = []
        observation = _latest_float(
            facts, asof, max_value=max_float, max_age_days=max_age, rejections=rejections
        )
        for reason, detail in rejections:
            log.warning(
                "%s (cik %s): rejected EntityPublicFloat observation: %s", ticker, cik, detail
            )
            # A rejected observation is an exclusion only if it left the candidate
            # with nothing to rank on; otherwise the candidate is seated on an
            # older value and this is an annotation, not a drop.
            (note if observation is not None else drop)(ticker, cik, reason, detail)
        if observation is None:
            drop(ticker, cik, "no_eligible_float_observation")
            continue
        value, observation_end, filed = observation
        seated_ciks[cik] = ticker
        candidates.append(FloatCandidate(ticker, cik, value, observation_end, filed, resolved_from))
    ranked = sorted(candidates, key=lambda item: (-item.public_float, item.ticker))
    return UniverseBuild(ranked, drops, notes)


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


def render_drop_manifest(
    drops: Sequence[UniverseDrop],
    *,
    notes: Sequence[UniverseDrop] = (),
    universe_id: str,
    asof: date,
    spec_sha256: str,
    source_sha256: str,
    max_public_float_usd: float,
    max_observation_age_days: float,
) -> str:
    """Render the companion artifact naming every candidate the rule declined.

    Published next to the membership file, never inside it: the membership format
    is hash-locked, is parsed by the existing loader, and carries no values or
    rank order.

    ``drops`` are exclusions and are disjoint from the seated universe, so the
    eligible pool really is the complement. ``notes`` records things worth knowing
    about names that WERE seated — a substituted filer CIK, an observation the
    bounds rejected while an older one still ranked the issuer. Keeping them apart
    matters: a ticker appearing in both the universe file and its drop list makes
    the two artifacts contradict each other.

    Disclosure note, stated rather than pretended away: this file names every
    candidate considered, so the eligible pool is reconstructable from it by
    complement. That is acceptable here because every input on this path — SEC
    companyfacts, SEC ticker directories, Nasdaq Trader listing files — is
    US-government or exchange public-domain data under the spec's own license
    note, and no Massive, FINRA, Finnhub, IBKR, SSGA or stockanalysis.com
    derivation reaches it. What this file must never carry is the SEATED members'
    float values or their rank order, and it does not.
    """
    payload = {
        "schema_version": "1.0",
        "universe_id": universe_id,
        "asof": asof.isoformat(),
        "spec_sha256": spec_sha256,
        "source_sha256": source_sha256,
        "bounds": {
            "max_public_float_usd": max_public_float_usd,
            "max_observation_age_days": max_observation_age_days,
        },
        "license_note": (
            "US-government public-domain source data (17 USC 105); derived work, "
            "freely redistributable."
        ),
        "drop_count": len(drops),
        "dropped_ticker_count": len({d.ticker for d in drops}),
        "reason_counts": {
            reason: sum(1 for d in drops if d.reason == reason)
            for reason in sorted({d.reason for d in drops})
        },
        "drops": [
            {"ticker": d.ticker, "cik": d.cik, "reason": d.reason, "detail": d.detail}
            for d in sorted(drops, key=lambda d: (d.reason, d.ticker))
        ],
        "note_count": len(notes),
        "notes": [
            {"ticker": d.ticker, "cik": d.cik, "reason": d.reason, "detail": d.detail}
            for d in sorted(notes, key=lambda d: (d.reason, d.ticker))
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def assert_archive_covers_asof(
    facts_source: _FactsSource, asof: date, *, max_lag_days: int = _ARCHIVE_MAX_LAG_DAYS
) -> str:
    """Refuse to build a dated universe from an archive that cannot cover that date.

    Two distinct failures, both silent before this check existed:

    * **Too early.** EDGAR keeps stamping ``filed = D`` until its filing window
      closes at 22:00 America/New_York. The cached 2026-07-30 archive was
      assembled at 16:18 UTC — around noon Eastern, hours before that day stopped
      producing filings — so a universe built from it as of 2026-07-30 is missing
      documents ``_latest_float`` treats as knowable. The committed 2026-07-30
      files were in fact built from the **2026-07-28** generation, which is why
      they no longer rebuild. The floor is therefore ``asof + 1 day at 03:00
      UTC``: 22:00 ET is never later than that instant (New York is never further
      from UTC than UTC-5), so the bound errs only toward refusing a compliant
      archive, never toward accepting an incomplete one.
    * **Too late.** An archive generated long after ``asof`` carries restatements
      and late filings. ``_latest_float`` still filters them by ``filed <= asof``,
      but the further the generation drifts the less the artifact describes the
      state of the world on its own as-of date, so ``max_lag_days`` bounds it.

    Returns the archive's ``Last-Modified`` header so callers can record it.
    """
    from datetime import UTC, datetime, time
    from email.utils import parsedate_to_datetime

    provenance = facts_source.provenance()
    if provenance is None:
        raise ValueError("SEC bulk provenance sidecar is unavailable")
    raw = provenance.get("last_modified")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("SEC bulk provenance is missing last_modified")
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"SEC bulk last_modified is not a valid HTTP date: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    generated = parsed.astimezone(UTC)

    floor = datetime.combine(asof + timedelta(days=1), time(3, 0), tzinfo=UTC)
    if generated < floor:
        raise ValueError(
            f"SEC Companyfacts archive generated {generated.isoformat()} cannot cover asof "
            f"{asof.isoformat()}: EDGAR accepts filings dated {asof.isoformat()} until "
            f"{floor.isoformat()}, so the archive is missing facts the selection rule treats "
            f"as knowable. Rebuild with an archive generated on or after that instant, or "
            f"select an earlier asof."
        )
    if generated.date() > asof + timedelta(days=max_lag_days):
        raise ValueError(
            f"SEC Companyfacts archive generated {generated.date().isoformat()} is more than "
            f"{max_lag_days} days after asof {asof.isoformat()}; it describes a later state "
            f"of EDGAR than the artifact claims"
        )
    return raw


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
    # Before anything is written: prove the archive can testify about this date.
    assert_archive_covers_asof(facts_source, asof)
    build = build_sec_float_universe_with_drops(source, facts_source, spec, asof)
    ranked = build.candidates
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
    # Written only after every membership file succeeded, so a build that refuses
    # partway cannot leave a manifest describing a universe that does not exist.
    # It covers the whole ranked pool, not one stage.
    _immutable_write(
        Path(out_dir) / f"universe_drops_{asof.isoformat()}.json",
        render_drop_manifest(
            build.drops,
            notes=build.notes,
            universe_id=universe_id,
            asof=asof,
            spec_sha256=spec_sha256,
            source_sha256=source_sha256,
            max_public_float_usd=_spec_positive_number(
                spec, "max_public_float_usd", _DEFAULT_MAX_PUBLIC_FLOAT_USD
            ),
            max_observation_age_days=_spec_positive_number(
                spec, "max_observation_age_days", _DEFAULT_MAX_OBSERVATION_AGE_DAYS
            ),
        ),
    )
    return outputs
