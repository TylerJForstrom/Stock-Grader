from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime
from email.utils import format_datetime
from pathlib import Path

import pytest

from stock_grader.data.sec import SECClient, SECProvider
from stock_grader.data.sec_bulk import SECBulkFacts, SECBulkFactsError
from stock_grader.data.sec_float_universe import (
    build_sec_float_universe,
    build_sec_float_universe_with_drops,
    emit_sec_float_universes,
    render_drop_manifest,
    universe_source_sha256,
)


def _zip_bytes(members: dict[str, dict]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, json.dumps(payload))
    return buffer.getvalue()


def _http_date(day: date | None = None) -> str:
    selected = day or datetime.now(UTC).date()
    return format_datetime(
        datetime(selected.year, selected.month, selected.day, 4, 30, tzinfo=UTC),
        usegmt=True,
    )


class StubClient:
    def __init__(
        self,
        content: bytes | None,
        *,
        last_modified: str | None = None,
        offline: bool = False,
    ) -> None:
        self.content = content
        self.last_modified = last_modified or _http_date()
        self.offline = offline
        self.head_calls = 0
        self.get_calls = 0

    def head(self, _url: str) -> dict[str, str] | None:
        self.head_calls += 1
        if self.content is None:
            return None

        return {
            "Content-Length": str(len(self.content)),
            "Last-Modified": self.last_modified,
        }

    def get_bytes(self, _url: str) -> bytes | None:
        self.get_calls += 1
        return self.content

    def get_bytes_with_headers(self, _url: str) -> tuple[bytes, dict[str, str]] | None:
        self.get_calls += 1
        if self.content is None:
            return None
        return self.content, {
            "Content-Length": str(len(self.content)),
            "Last-Modified": self.last_modified,
        }


def test_sec_client_head_uses_the_shared_fair_access_session(tmp_path: Path) -> None:
    expected_headers = {"Content-Length": "123", "Last-Modified": _http_date()}

    class Response:
        status_code = 200
        content = b"archive-bytes"

        def __init__(self) -> None:
            self.headers = expected_headers

    class Session:
        def __init__(self) -> None:
            self.head_calls: list[tuple[str, float, bool, dict[str, str]]] = []
            self.get_calls: list[tuple[str, float, dict[str, str]]] = []

        def head(
            self,
            url: str,
            *,
            timeout: float,
            allow_redirects: bool,
            headers: dict[str, str],
        ):
            self.head_calls.append((url, timeout, allow_redirects, headers))
            return Response()

        def get(self, url: str, *, timeout: float, headers: dict[str, str]):
            self.get_calls.append((url, timeout, headers))
            return Response()

    client = SECClient(cache_dir=tmp_path, contact="tests@example.com", rate=1_000_000)
    session = Session()
    client._session = session

    assert client.head(SECBulkFacts.URL) == expected_headers
    assert client.get_bytes(SECBulkFacts.URL) == b"archive-bytes"
    assert client.get_bytes_with_headers(SECBulkFacts.URL) == (
        b"archive-bytes",
        expected_headers,
    )
    identity = {"Accept-Encoding": "identity"}
    assert session.head_calls == [(SECBulkFacts.URL, client.timeout, True, identity)]
    assert session.get_calls == [
        (SECBulkFacts.URL, 180.0, identity),
        (SECBulkFacts.URL, 180.0, identity),
    ]

    offline = SECClient(cache_dir=tmp_path / "offline", contact="tests@example.com", offline=True)
    offline._session = session
    assert offline.head(SECBulkFacts.URL) is None
    assert offline.get_bytes(SECBulkFacts.URL) is None
    assert offline.get_bytes_with_headers(SECBulkFacts.URL) is None
    assert len(session.head_calls) == 1
    assert len(session.get_calls) == 2


def _write_cached(
    cache: Path,
    content: bytes,
    *,
    day: date | None = None,
) -> tuple[Path, dict]:
    selected = day or datetime.now(UTC).date()
    archive = cache / f"companyfacts_{selected.isoformat()}.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(content)
    with zipfile.ZipFile(io.BytesIO(content)) as fixture:
        member_count = len(fixture.infolist())
    sidecar = {
        "url": SECBulkFacts.URL,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "last_modified": _http_date(selected),
        "fetched_at_utc": datetime.now(UTC).isoformat(),
        "member_count": member_count,
    }
    archive.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")
    return archive, sidecar


def test_current_cached_zip_and_valid_sidecar_make_zero_network_calls(tmp_path: Path) -> None:
    content = _zip_bytes({"CIK0000000001.json": {"cik": 1, "facts": {}}})
    archive, _ = _write_cached(tmp_path, content)
    old_archive, _ = _write_cached(tmp_path, content, day=date(2025, 1, 2))
    old_sidecar = old_archive.with_suffix(".json")

    class NoNetwork:
        offline = False

        def head(self, _url: str):
            raise AssertionError("same-day valid cache must not issue HEAD")

        def get_bytes(self, _url: str):
            raise AssertionError("same-day valid cache must not issue GET")

    bulk = SECBulkFacts(NoNetwork(), cache_dir=tmp_path)
    assert bulk.ensure() == archive
    assert bulk.company_facts("1") == {"cik": 1, "facts": {}}
    assert not old_archive.exists()
    assert not old_sidecar.exists()


def test_missing_member_returns_none_and_provider_falls_back_per_cik(tmp_path: Path) -> None:
    content = _zip_bytes({"CIK0000000001.json": {"cik": 1, "facts": {}}})
    _write_cached(tmp_path, content)
    bulk = SECBulkFacts(StubClient(None, offline=True), cache_dir=tmp_path)
    assert bulk.company_facts("2") is None

    class PerCikClient:
        def __init__(self) -> None:
            self.facts_calls: list[tuple[str, bool]] = []

        def submissions(self, _cik: str, *, refresh: bool = False):
            return {}

        def company_facts(self, cik: str, *, refresh: bool = False):
            self.facts_calls.append((cik, refresh))
            return {"facts": {}}

    client = PerCikClient()
    snapshot = SECProvider(client, bulk=bulk).fetch_by_cik("2", ticker="MISS")
    assert snapshot.fundamentals is not None
    assert client.facts_calls == [("0000000002", False)]


def test_download_removes_older_generations_and_writes_provenance(tmp_path: Path) -> None:
    old_content = _zip_bytes({"CIK0000000001.json": {"old": True}})
    old_archive, _ = _write_cached(tmp_path, old_content, day=date(2020, 1, 2))
    content = _zip_bytes(
        {
            "CIK0000000001.json": {"cik": 1},
            "CIK0000000002.json": {"cik": 2},
        }
    )
    client = StubClient(content)
    bulk = SECBulkFacts(client, cache_dir=tmp_path)
    archive = bulk.ensure()

    assert archive is not None and archive.exists()
    assert client.head_calls == 1
    assert client.get_calls == 1
    assert not old_archive.exists()
    assert not old_archive.with_suffix(".json").exists()
    sidecar = json.loads(archive.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar == {
        "bytes": len(content),
        "fetched_at_utc": sidecar["fetched_at_utc"],
        "last_modified": client.last_modified,
        "member_count": 2,
        "sha256": hashlib.sha256(content).hexdigest(),
        "url": SECBulkFacts.URL,
    }


def test_matching_generation_after_head_avoids_download(tmp_path: Path) -> None:
    content = _zip_bytes({"CIK0000000001.json": {"cik": 1}})
    yesterday = datetime.now(UTC).date().replace(year=2025)
    archive, _ = _write_cached(tmp_path, content, day=yesterday)
    client = StubClient(content, last_modified=_http_date(yesterday))

    assert SECBulkFacts(client, cache_dir=tmp_path).ensure() == archive
    assert client.head_calls == 1
    assert client.get_calls == 0


def test_download_validates_the_get_generation_when_head_is_ahead(
    tmp_path: Path,
) -> None:
    """SEC's CDN can publish a new HEAD while GET still serves the prior valid ZIP."""
    content = _zip_bytes({"CIK0000000001.json": {"cik": 1}})
    get_modified = _http_date(date(2026, 7, 30))

    class SplitGeneration(StubClient):
        def head(self, _url: str) -> dict[str, str]:
            self.head_calls += 1
            return {
                "Content-Length": str(len(content) + 327_655),
                "Last-Modified": _http_date(date(2026, 7, 31)),
            }

        def get_bytes_with_headers(self, _url: str) -> tuple[bytes, dict[str, str]]:
            self.get_calls += 1
            return content, {
                "Content-Length": str(len(content)),
                "Last-Modified": get_modified,
            }

    client = SplitGeneration(content)
    archive = SECBulkFacts(client, cache_dir=tmp_path).ensure()
    assert archive == tmp_path / "companyfacts_2026-07-30.zip"
    assert archive is not None and archive.read_bytes() == content
    sidecar = json.loads(archive.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["bytes"] == len(content)
    assert sidecar["last_modified"] == get_modified


def test_offline_uses_cached_zip_and_missing_cache_returns_none(tmp_path: Path) -> None:
    empty = SECBulkFacts(StubClient(None, offline=True), cache_dir=tmp_path / "empty")
    assert empty.ensure() is None
    content = _zip_bytes({"CIK0000000001.json": {"cik": 1}})
    archive, _ = _write_cached(tmp_path / "cached", content, day=date(2025, 1, 2))
    cached = SECBulkFacts(StubClient(None, offline=True), cache_dir=tmp_path / "cached")
    assert cached.ensure() == archive


@pytest.mark.parametrize("bad_value", ["not-a-number", "1.5", "0"])
def test_required_numeric_content_length_fails_closed(tmp_path: Path, bad_value: str) -> None:
    class BadHeaders(StubClient):
        def head(self, _url: str) -> dict[str, str]:
            return {"Content-Length": bad_value, "Last-Modified": _http_date()}

    with pytest.raises(SECBulkFactsError, match="Content-Length"):
        SECBulkFacts(BadHeaders(b"x"), cache_dir=tmp_path).ensure()


class FixtureSymbols:
    def __init__(self) -> None:
        self.records = [
            {"ticker": "BRK.A", "cik": 3, "exchange": "NYSE"},
            {"ticker": "AAA", "cik": 1, "exchange": "NYSE"},
            {"ticker": "BBB", "cik": 2, "exchange": "Nasdaq"},
            {"ticker": "BRK.B", "cik": 3, "exchange": "NYSE"},
            {"ticker": "ETF", "cik": 4, "exchange": "NYSE"},
            {"ticker": "TEST", "cik": 5, "exchange": "NYSE"},
            {"ticker": "NOFLAG", "cik": 6, "exchange": "NYSE"},
        ]
        self.manifest_salt = "1" * 64
        self.directory_asof: list[str | None] = []

    def universe(self, *, listed_only: bool = True, asof: str | None = None):
        assert asof == "2026-07-31"
        return self.records

    def symbol_directory(self, name: str, *, asof: str | None = None):
        self.directory_asof.append(asof)
        if name == "nasdaqlisted.jsonl":
            return [
                {"ticker": "AAA", "etf": "N", "test_issue": "N"},
                {"ticker": "BBB", "etf": "N", "test_issue": "N"},
                {"ticker": "TEST", "etf": "N", "test_issue": "Y"},
            ]
        return [
            {"ticker": "BRK.B", "etf": "N", "test_issue": "N"},
            {"ticker": "BRK.A", "etf": "N", "test_issue": "N"},
            {"ticker": "ETF", "etf": "Y", "test_issue": "N"},
        ]

    def manifest(self, dataset_dir: str):
        return {
            "schema_version": "1.0",
            "dataset": dataset_dir,
            "files": [{"name": f"{dataset_dir}.fixture", "sha256": self.manifest_salt, "bytes": 1}],
        }


def _float_facts(value: float, *, future: float | None = None) -> dict:
    records = [{"end": "2026-06-30", "filed": "2026-07-15", "val": value}]
    if future is not None:
        records.append({"end": "2026-09-30", "filed": "2026-10-15", "val": future})
    return {"facts": {"dei": {"EntityPublicFloat": {"units": {"USD": records}}}}}


class FixtureFacts:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bulk_sha = "a" * 64
        self.last_modified = "Sat, 01 Aug 2026 08:00:00 GMT"
        self.rows = {
            "0000000001": _float_facts(100.0),
            "0000000002": _float_facts(50.0, future=500.0),
            "0000000003": _float_facts(75.0),
            "0000000004": _float_facts(1_000.0),
            "0000000005": _float_facts(900.0),
            "0000000006": _float_facts(2_000.0),
        }

    def company_facts(self, cik: str):
        self.calls.append(cik)
        return self.rows.get(cik)

    def provenance(self):
        # Generated after EDGAR stopped accepting filings dated 2026-07-31, which
        # is what assert_archive_covers_asof requires of a 2026-07-31 universe.
        return {"sha256": self.bulk_sha, "last_modified": self.last_modified}


def _spec() -> dict:
    return {
        "schema_version": "1.0",
        "universe_id": "liq1000_v1",
        "rank_key": "dei:EntityPublicFloat",
        "exclude_etf": True,
        "exclude_test_issue": True,
        "target_size": 3,
        "issuer_ticker_rule": "one_per_cik_sec_dash_ticker_ascending",
        "require_sec_cik": True,
        "listed_exchanges": ["NYSE", "Nasdaq"],
    }


def test_sec_float_generator_is_pit_filters_flags_and_emits_only_alpha_membership(
    tmp_path: Path,
) -> None:
    facts = FixtureFacts()
    ranked = build_sec_float_universe(FixtureSymbols(), facts, _spec(), date(2026, 7, 31))
    assert [item.ticker for item in ranked] == ["AAA", "BRK-A", "BBB"]
    assert [item.public_float for item in ranked] == [100.0, 75.0, 50.0]
    assert facts.calls.count("0000000003") == 1

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec()), encoding="utf-8")
    outputs = emit_sec_float_universes(
        FixtureSymbols(),
        FixtureFacts(),
        spec_path,
        date(2026, 7, 31),
        tmp_path / "out",
        sizes=(2, 3),
    )
    lines = outputs[3].read_text(encoding="utf-8").splitlines()
    assert lines[-3:] == ["AAA", "BBB", "BRK-A"]
    assert "rank" not in outputs[3].read_text(encoding="utf-8").lower()
    assert "public_float" not in outputs[3].read_text(encoding="utf-8").lower()

    assert "# universe_id: liq1000_v1" in outputs[3].read_text(encoding="utf-8")
    assert "# universe_id: liq1000_v1_stage2" in outputs[2].read_text(encoding="utf-8")


def test_universe_source_sha256_covers_bulk_and_both_foundry_manifests() -> None:
    symbols = FixtureSymbols()
    facts = FixtureFacts()
    baseline = universe_source_sha256(symbols, facts)
    assert baseline != facts.bulk_sha

    symbols.manifest_salt = "b" * 64
    foundry_changed = universe_source_sha256(symbols, facts)
    assert foundry_changed != baseline

    symbols.manifest_salt = "1" * 64
    facts.bulk_sha = "c" * 64
    bulk_changed = universe_source_sha256(symbols, facts)
    assert bulk_changed != baseline


@pytest.mark.parametrize("bad_value", ["bad", True])
def test_sec_float_generator_refuses_unparseable_values(bad_value: object) -> None:
    facts = FixtureFacts()
    facts.rows["0000000001"] = _float_facts(100.0)
    facts.rows["0000000001"]["facts"]["dei"]["EntityPublicFloat"]["units"]["USD"][0]["val"] = (
        bad_value
    )
    with pytest.raises(ValueError, match="not numeric"):
        build_sec_float_universe(FixtureSymbols(), facts, _spec(), date(2026, 7, 31))


# --- audit regressions -------------------------------------------------------
#
# One test per confirmed defect in the codex/m5-wide-universe audit. Each fails
# on the pre-fix implementation; the failure mode is named in each docstring.


class PitFixtureSymbols(FixtureSymbols):
    """Symbol source whose listing directories moved AFTER the selection date.

    ``GONE`` was listed on 2026-07-31 and removed from nasdaqlisted on 08-01, so
    today's directory no longer carries it. ``LATE`` was added on 08-01 and must
    not appear in a 07-31 universe.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records = [
            {"ticker": "GONE", "cik": 7, "exchange": "NYSE"},
            {"ticker": "STAY", "cik": 8, "exchange": "NYSE"},
        ]

    def universe(self, *, listed_only: bool = True, asof: str | None = None):
        assert asof == "2026-07-31"
        return self.records

    def symbol_directory(self, name: str, *, asof: str | None = None):
        self.directory_asof.append(asof)
        today = (
            [{"ticker": "STAY", "etf": "N", "test_issue": "N", "name": "Stay Inc. Common Stock"}]
            if name == "nasdaqlisted.jsonl"
            else []
        )
        if asof is None:
            return today
        replayed = list(today)
        for event in self.events():
            if event["source"] != name.removesuffix(".jsonl") or event["date"] <= asof:
                continue
            if event["event"] == "removed":
                replayed.append(event["record"])
            elif event["event"] == "added":
                replayed = [r for r in replayed if r["ticker"] != event["record"]["ticker"]]
        return replayed

    def events(self):
        return [
            {
                "date": "2026-08-01",
                "event": "removed",
                "source": "nasdaqlisted",
                "record": {
                    "ticker": "GONE",
                    "etf": "N",
                    "test_issue": "N",
                    "name": "Gone Corp. Common Stock",
                },
            }
        ]


def test_universe_membership_is_screened_against_the_asof_listing_directory() -> None:
    """Defect 1: a name delisted after asof must not vanish from a dated universe.

    Pre-fix, ``_listing_flags`` read ``data/symbols/current`` with no replay and
    bare-``continue``d every candidate missing from it, so GONE — listed on the
    as-of date, delisted the day after — silently disappeared. That is the
    survivorship bias the milestone exists to remove.
    """
    facts = FixtureFacts()
    facts.rows["0000000007"] = _float_facts(900.0)
    facts.rows["0000000008"] = _float_facts(100.0)
    symbols = PitFixtureSymbols()

    ranked = build_sec_float_universe(symbols, facts, _spec(), date(2026, 7, 31))

    assert [item.ticker for item in ranked] == ["GONE", "STAY"]
    # The screen must be asked for the as-of directory, never today's.
    assert symbols.directory_asof and set(symbols.directory_asof) == {"2026-07-31"}


def test_emit_refuses_an_archive_generated_before_the_asof_filing_window_closed(
    tmp_path: Path,
) -> None:
    """Defect 2: the archive generation must be checked against asof.

    Pre-fix nothing compared the two, so the committed 2026-07-30 files were built
    from the 2026-07-28 generation and no longer rebuild. EDGAR keeps stamping
    ``filed = D`` until 22:00 America/New_York, so an archive generated at noon
    Eastern on D is missing filings the rule treats as knowable on D.
    """
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec()), encoding="utf-8")
    facts = FixtureFacts()
    facts.last_modified = "Fri, 31 Jul 2026 16:18:54 GMT"  # noon ET on the as-of date

    with pytest.raises(ValueError, match="cannot cover asof"):
        emit_sec_float_universes(
            PitFixtureSymbols(), facts, spec_path, date(2026, 7, 31), tmp_path / "out"
        )
    assert not (tmp_path / "out").exists()  # nothing written on refusal


def test_rank_observations_are_bounded_for_plausibility_and_recency() -> None:
    """Defect 3: an absurd or ancient float must not set an issuer's rank.

    Pre-fix ``_required_float`` accepted any finite positive value and
    ``_latest_float`` accepted any observation on or before asof, so Cabot Corp's
    $4.43 QUADRILLION cover-page tag ranked it first in the committed N=1000
    universe and 2009 floats still ranked live issuers.
    """
    facts = FixtureFacts()
    # AAA files an implausible value now and a sane one a year earlier.
    facts.rows["0000000001"] = {
        "facts": {
            "dei": {
                "EntityPublicFloat": {
                    "units": {
                        "USD": [
                            {"end": "2025-06-30", "filed": "2025-07-15", "val": 100.0},
                            {"end": "2026-06-30", "filed": "2026-07-15", "val": 4.43e15},
                        ]
                    }
                }
            }
        }
    }
    # BBB's only observation is sixteen years stale.
    facts.rows["0000000002"] = {
        "facts": {
            "dei": {
                "EntityPublicFloat": {
                    "units": {"USD": [{"end": "2009-12-31", "filed": "2010-02-01", "val": 5000.0}]}
                }
            }
        }
    }

    build = build_sec_float_universe_with_drops(FixtureSymbols(), facts, _spec(), date(2026, 7, 31))

    seated = {item.ticker: item.public_float for item in build.candidates}
    assert seated["AAA"] == 100.0, "the quadrillion tag must not become the rank key"
    assert "BBB" not in seated, "a 2009 float cannot rank a 2026 issuer"

    # AAA is still seated on its older sane value, so the ceiling breach is an
    # annotation; BBB has nothing left to rank on, so it is a genuine exclusion.
    assert ("AAA", "float_exceeds_plausibility_ceiling") in {
        (d.ticker, d.reason) for d in build.notes
    }
    assert ("BBB", "float_observation_too_stale") in {(d.ticker, d.reason) for d in build.drops}
    # Loud, not silent: every rejection carries the values that triggered it.
    assert all(d.detail for d in [*build.drops, *build.notes] if d.reason.startswith("float_"))
    # A seated ticker must never appear as an exclusion.
    assert not ({c.ticker for c in build.candidates} & {d.ticker for d in build.drops})


def test_dropped_and_substituted_listings_are_recorded_in_a_manifest() -> None:
    """Defect 4: XOM-class losses and one-per-CIK substitutions must be visible.

    Pre-fix, a CIK whose companyfacts member carried no ``dei`` block hit a bare
    ``continue`` — which is how ExxonMobil, whose ticker SEC reassigned to a
    holding-company shell, left a thousand-name universe without a trace — and the
    security that lost a one-per-CIK contest was never named anywhere.
    """
    facts = FixtureFacts()
    build = build_sec_float_universe_with_drops(FixtureSymbols(), facts, _spec(), date(2026, 7, 31))

    by_reason = {d.reason for d in build.drops}
    assert "duplicate_cik" in by_reason, "the displaced sibling listing must be named"
    displaced = next(d for d in build.drops if d.reason == "duplicate_cik")
    assert displaced.ticker == "BRK-B"
    assert "retained_ticker=BRK-A" in displaced.detail
    # ETF and test-issue exclusions are recorded too, not merely applied.
    assert {"excluded_etf", "excluded_test_issue"} <= by_reason
    # Published CIKs are uniformly zero-padded strings, never a mix of int and str.
    assert all(d.cik is None or (isinstance(d.cik, str) and len(d.cik) == 10) for d in build.drops)
    # The reason the whole manifest exists: a CIK with no dei block is named, not
    # swallowed by a bare continue. NOFLAG has no listing-directory row.
    assert "listing_directory_row_missing" in by_reason

    rendered = json.loads(
        render_drop_manifest(
            build.drops,
            notes=build.notes,
            universe_id="liq1000_v1",
            asof=date(2026, 7, 31),
            spec_sha256="0" * 64,
            source_sha256="1" * 64,
            max_public_float_usd=1e13,
            max_observation_age_days=730,
        )
    )
    assert rendered["drop_count"] == len(build.drops)
    assert rendered["reason_counts"]["duplicate_cik"] >= 1
    # The schema itself carries no float value and no rank position for any member,
    # which is what makes the artifact publishable next to the membership file.
    assert all(
        set(d) == {"ticker", "cik", "reason", "detail"}
        for d in [*rendered["drops"], *rendered["notes"]]
    )
    # Exclusions are disjoint from the universe, so the pool is the complement.
    assert not (
        {item.ticker for item in build.candidates} & {d["ticker"] for d in rendered["drops"]}
    )


def test_filer_cik_substitution_recovers_an_issuer_behind_a_registrant_shell() -> None:
    """Defect 4: fall back to the CIK that actually files, and say so.

    ``NEWCO`` is the directory CIK for an issuer SEC has re-registered; its
    companyfacts member carries only registration taxonomies. The operating
    company files under a different CIK with the same registrant name.
    """

    class ShellSymbols(FixtureSymbols):
        def __init__(self) -> None:
            super().__init__()
            self.records = [{"ticker": "NEW", "cik": 9, "exchange": "NYSE", "title": "Newco Corp"}]

        def universe(self, *, listed_only: bool = True, asof: str | None = None):
            return self.records

        def symbol_directory(self, name: str, *, asof: str | None = None):
            self.directory_asof.append(asof)
            if name == "nasdaqlisted.jsonl":
                return [
                    {
                        "ticker": "NEW",
                        "etf": "N",
                        "test_issue": "N",
                        "name": "Newco Corporation Common Stock",
                    }
                ]
            return []

    class ShellFacts(FixtureFacts):
        def __init__(self) -> None:
            super().__init__()
            shell = {"entityName": "NEWCO INDUSTRIES CORP", "facts": {"ffd": {"NetFeeAmt": {}}}}
            filer = _float_facts(750.0) | {"entityName": "Newco Industries Corporation"}
            self.rows = {"0000000009": shell, "0000000099": filer}

        def entity_name_index(self):
            return {"NEWCOINDUSTRIES": ["0000000009", "0000000099"]}

    build = build_sec_float_universe_with_drops(
        ShellSymbols(), ShellFacts(), _spec(), date(2026, 7, 31)
    )

    assert [item.ticker for item in build.candidates] == ["NEW"]
    seated = build.candidates[0]
    assert seated.cik == "0000000099"
    assert seated.resolved_from_cik == "0000000009"
    substitution = next(d for d in build.notes if d.reason == "filer_cik_substituted")
    # Recorded as an inference, not attested as proven succession.
    assert "unattested" in substitution.detail
    assert "seated_cik=0000000099" in substitution.detail
    # NEW is in the universe, so it must not also be listed as an exclusion.
    assert "NEW" not in {d.ticker for d in build.drops}


def test_cached_archive_is_rejected_when_its_bytes_do_not_match_the_recorded_sha256(
    tmp_path: Path,
) -> None:
    """Defect 6: byte length is not integrity.

    Pre-fix every cache-reuse path compared only ``stat().st_size`` to the
    sidecar's ``bytes``, so a same-length forged archive was accepted and its
    unverified digest was published as ``source_sha256`` in the public universe
    files. Recompute and compare instead.
    """
    genuine = _zip_bytes({"CIK0000000001.json": {"cik": "1", "entityName": "Real Corp"}})
    forged = _zip_bytes({"CIK0000000001.json": {"cik": "1", "entityName": "Fake Corp"}})
    # A forgery is only interesting if it survives the length check.
    forged += b"\x00" * (len(genuine) - len(forged)) if len(forged) < len(genuine) else b""
    assert len(forged) == len(genuine), "fixture must isolate digest checking from length checking"

    archive = tmp_path / "companyfacts_2026-07-30.zip"
    archive.write_bytes(forged)
    (tmp_path / "companyfacts_2026-07-30.json").write_text(
        json.dumps(
            {
                "url": SECBulkFacts.URL,
                "bytes": len(genuine),
                "sha256": hashlib.sha256(genuine).hexdigest(),  # the digest of the REAL archive
                "last_modified": _http_date(),
                "member_count": 1,
            }
        ),
        encoding="utf-8",
    )

    bulk = SECBulkFacts(StubClient(None, offline=True), cache_dir=tmp_path)
    with pytest.raises(SECBulkFactsError, match="does not match its recorded sha256"):
        bulk.ensure()


def test_second_signal_rejections_are_opt_in_and_catch_scale_errors() -> None:
    """A scalar ceiling cannot separate a $4T scale error from a real $4T megacap.

    Measured on the live archive: every corrupt cover-page float is >=1,260x the
    issuer's annual revenue (real issuers peak ~135x), and the low-revenue
    stragglers jump >2,000x their own recent median (genuine growth peaks ~11x).
    Cabot Corp's $4.43 quadrillion tag ranked FIRST under v1.
    """
    facts = FixtureFacts()
    # AAA: corrupt newest observation (1000x its own history, 100000x revenue),
    # sane older ones — must seat on the older value.
    history = [
        {"end": f"202{i}-06-30", "filed": f"202{i}-07-15", "val": 4.0e9 + i * 1e8}
        for i in range(3, 6)
    ]
    corrupt = {
        "end": "2026-06-30",
        "filed": "2026-07-15",
        "val": 4.4e12,
    }  # UNDER the ceiling: only a second signal can catch it
    facts.rows["0000000001"] = {
        "facts": {
            "dei": {"EntityPublicFloat": {"units": {"USD": [*history, corrupt]}}},
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-01",
                                "val": 4.0e9,
                            }
                        ]
                    }
                }
            },
        }
    }

    spec = {
        **_spec(),
        "max_float_to_revenue": 500,
        "max_float_jump_vs_recent": 1000,
    }
    build = build_sec_float_universe_with_drops(FixtureSymbols(), facts, spec, date(2026, 7, 31))
    seated = {c.ticker: c for c in build.candidates}
    assert seated["AAA"].public_float == pytest.approx(4.5e9), (
        "the corrupt observation must be rejected and the sane older one seated"
    )
    reasons = {n.reason for n in build.notes if n.ticker == "AAA"}
    assert reasons & {"float_implausible_vs_revenue", "float_jump_vs_history"}

    # Without the opt-in keys (the immutable v1 spec), behavior is unchanged:
    # the corrupt value wins because it passes the bare ceiling. That is the
    # documented v1 defect, preserved so v1 rebuilds stay reproducible.
    v1 = build_sec_float_universe_with_drops(FixtureSymbols(), facts, _spec(), date(2026, 7, 31))
    assert {c.ticker: c.public_float for c in v1.candidates}["AAA"] == pytest.approx(4.4e12)


def test_missing_revenue_skips_the_cross_check_rather_than_failing() -> None:
    """Absence of the cross-check datum is not evidence of corruption."""
    facts = FixtureFacts()  # fixture issuers file no revenue at all
    spec = {**_spec(), "max_float_to_revenue": 500}
    build = build_sec_float_universe_with_drops(FixtureSymbols(), facts, spec, date(2026, 7, 31))
    assert build.candidates, "no-revenue issuers must still seat on the ceiling alone"
