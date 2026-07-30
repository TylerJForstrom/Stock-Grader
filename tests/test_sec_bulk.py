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
    emit_sec_float_universes,
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

        def get_bytes_with_headers(
            self, _url: str
        ) -> tuple[bytes, dict[str, str]]:
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

    def universe(self, *, listed_only: bool = True, asof: str | None = None):
        assert asof == "2026-07-31"
        return self.records

    def symbol_directory(self, name: str):
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
        return {"sha256": self.bulk_sha}


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
