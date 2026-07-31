"""Selective access to the SEC's daily bulk Companyfacts archive.

The grader already owns SEC EDGAR as its primary fundamentals source, and its insider-price
provider already consumes SEC bulk ZIPs through :class:`~stock_grader.data.sec.SECClient`.
Keeping Companyfacts here preserves that ownership and the one-direction ecosystem DAG. Publishing
roughly 4.8 GB of extracted issuer JSON through the foundry would not be a git-sized artifact.

The working URL is ``daily-index/xbrl/companyfacts.zip``. The older
``daily-index/bulkdata/companyfacts.zip`` path returns AccessDenied and must not be restored.

A valid sidecar from the current UTC day is a deliberate zero-network fast path. Otherwise
``ensure`` performs one rate-limited HEAD to discover the generation and reuses a size-matching
archive without another GET. This reconciles daily freshness with cheap repeated invocations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from .cache import default_cache_dir
from .sec import SECClient

log = logging.getLogger(__name__)

__all__ = ["SECBulkFacts", "SECBulkFactsError"]

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ARCHIVE_NAME = re.compile(r"^companyfacts_(\d{4}-\d{2}-\d{2})\.zip$")
_ARCHIVE_MEMBER_CIK = re.compile(r"^CIK(\d{10})\.json$")
_ENTITY_NAME = re.compile(r'"entityName"\s*:\s*"((?:[^"\\]|\\.)*)"')
# entityName sits in the first object of every member, well inside this window.
_ENTITY_NAME_PROBE_BYTES = 512


class SECBulkFactsError(RuntimeError):
    """A bulk archive or its provenance metadata failed closed validation."""


class _BulkClient(Protocol):
    offline: bool

    def head(self, url: str) -> dict[str, str] | None: ...

    def get_bytes(self, url: str) -> bytes | None: ...

    def get_bytes_with_headers(self, url: str) -> tuple[bytes, dict[str, str]] | None: ...


def _atomic_bytes(destination: Path, content: bytes) -> None:
    """Atomically replace ``destination`` with bytes written in the same directory."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tmp",
            prefix=f".{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _positive_int(value: object, field: str) -> int:
    """Parse a required positive integer without defaulting malformed feed data."""
    if isinstance(value, bool):
        raise SECBulkFactsError(f"{field} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise SECBulkFactsError(f"{field} must be a positive integer")
    if parsed <= 0:
        raise SECBulkFactsError(f"{field} must be a positive integer")
    return parsed


def _header(headers: dict[str, str], name: str) -> str:
    values = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    value = values.get(name.lower())
    if not value:
        raise SECBulkFactsError(f"SEC HEAD omitted required {name} header")
    return value


class SECBulkFacts:
    """One-process, selectively-read view of the SEC Companyfacts ZIP."""

    URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

    def __init__(
        self,
        client: _BulkClient | None = None,
        *,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.client = client or SECClient()
        self.cache_dir = Path(cache_dir or default_cache_dir("bulk")).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._archive: zipfile.ZipFile | None = None
        self._archive_path: Path | None = None
        self._ensured = False
        self._ensured_path: Path | None = None
        # path -> digest already re-verified in THIS process. The threat is a
        # forged archive appearing between processes, so a fresh process always
        # re-hashes; within one process the file is not re-read.
        self._verified_digests: dict[Path, str] = {}
        self._entity_names: dict[str, list[str]] | None = None

    @staticmethod
    def _last_modified(value: str) -> tuple[str, datetime]:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise SECBulkFactsError("Last-Modified is not a valid HTTP date") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        return parsed.date().isoformat(), parsed

    @staticmethod
    def _sidecar_path(archive: Path) -> Path:
        return archive.with_suffix(".json")

    def _file_digest(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _verify_recorded_digest(self, archive: Path, expected: str) -> None:
        """Re-hash a cached archive and compare against its recorded sha256.

        Byte length is not integrity. The sidecar's digest is published verbatim
        as ``source_sha256`` in the public universe artifacts, so accepting a
        same-length archive on the strength of ``stat().st_size`` alone means an
        unverified digest becomes the provenance of a public file. Recompute
        instead — measured at well under a second for the real 1.39 GB archive, and
        memoised per process so a repeated ``ensure()`` does not pay it twice.

        A mismatch means the cache is corrupt or tampered with, so it raises
        rather than silently re-downloading: a provenance-publishing system must
        report that its inputs were not what it recorded, not quietly repair
        itself and carry on.
        """
        if self._verified_digests.get(archive) == expected:
            return
        actual = self._file_digest(archive)
        if actual != expected:
            raise SECBulkFactsError(
                f"SEC Companyfacts archive {archive} does not match its recorded sha256: "
                f"sidecar says {expected}, file hashes to {actual}. The cached archive is "
                f"corrupt or was replaced; refusing to publish its digest as provenance."
            )
        self._verified_digests[archive] = expected

    def _read_sidecar(self, archive: Path) -> dict[str, Any] | None:
        sidecar_path = self._sidecar_path(archive)
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("url") != self.URL:
            return None
        try:
            expected_bytes = _positive_int(payload.get("bytes"), "sidecar bytes")
            member_count = _positive_int(payload.get("member_count"), "sidecar member_count")
            _, modified = self._last_modified(str(payload.get("last_modified", "")))
        except SECBulkFactsError:
            return None
        digest = str(payload.get("sha256", ""))
        if not _SHA256.fullmatch(digest):
            return None
        try:
            actual_bytes = archive.stat().st_size
        except OSError:
            return None
        if actual_bytes != expected_bytes:
            return None
        self._verify_recorded_digest(archive, digest.lower())
        payload["bytes"] = expected_bytes
        payload["member_count"] = member_count
        payload["_last_modified_date"] = modified.date().isoformat()
        return payload

    def _cached_archives(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self.cache_dir.glob("companyfacts_*.zip")
                if _ARCHIVE_NAME.fullmatch(path.name)
            ),
            reverse=True,
        )

    def _same_day_fast_path(self) -> Path | None:
        today = datetime.now(UTC).date().isoformat()
        for archive in self._cached_archives():
            sidecar = self._read_sidecar(archive)
            if sidecar is not None and sidecar["_last_modified_date"] == today:
                return archive
        return None

    def _offline_cache(self) -> Path | None:
        """Use the newest locally valid ZIP while offline, even with an older sidecar."""
        for archive in self._cached_archives():
            sidecar = self._read_sidecar(archive)
            if sidecar is not None:
                return archive
            try:
                with zipfile.ZipFile(archive) as candidate:
                    if candidate.infolist():
                        return archive
            except (OSError, zipfile.BadZipFile):
                continue
        return None

    def entity_name_index(self) -> dict[str, list[str]]:
        """Map normalised registrant name -> sorted CIKs reporting under it.

        Reads only the leading bytes of each member, so ``entityName`` is
        recovered without decompressing ~4.8 GB of facts (measured: about 1.5
        seconds over 20,122 members). Used to find the operating company behind a
        holding-company shell whose ticker SEC has already reassigned.
        """
        if self._entity_names is not None:
            return self._entity_names
        index: dict[str, list[str]] = {}
        path = self.ensure()
        if path is None:
            self._entity_names = index
            return index
        from .sec_float_universe import normalized_entity_name

        archive = self._open(path)
        for member in archive.namelist():
            matched = _ARCHIVE_MEMBER_CIK.fullmatch(member)
            if matched is None:
                continue
            with archive.open(member) as handle:
                head = handle.read(_ENTITY_NAME_PROBE_BYTES).decode("utf-8", "ignore")
            found = _ENTITY_NAME.search(head)
            if found is None:
                continue
            try:
                raw = json.loads(f'"{found.group(1)}"')
            except json.JSONDecodeError:
                continue
            key = normalized_entity_name(raw)
            if not key:
                continue
            index.setdefault(key, []).append(matched.group(1))
        self._entity_names = {key: sorted(value) for key, value in index.items()}
        return self._entity_names

    @staticmethod
    def _archive_metadata(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        try:
            with zipfile.ZipFile(path) as archive:
                members = len(archive.infolist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise SECBulkFactsError(f"invalid SEC Companyfacts ZIP: {path}") from exc
        if members <= 0:
            raise SECBulkFactsError("SEC Companyfacts ZIP contains no members")
        return digest.hexdigest(), members

    def _write_sidecar(
        self,
        archive: Path,
        *,
        last_modified: str,
        digest: str,
        member_count: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": self.URL,
            "bytes": archive.stat().st_size,
            "sha256": digest,
            "last_modified": last_modified,
            "fetched_at_utc": datetime.now(UTC).isoformat(),
            "member_count": member_count,
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_bytes(self._sidecar_path(archive), encoded)
        return payload

    def _remove_old_generations(self, keep: Path) -> None:
        for archive in self._cached_archives():
            if archive == keep:
                continue
            try:
                archive.unlink()
                self._sidecar_path(archive).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("could not remove old SEC bulk generation %s: %s", archive, exc)

    def ensure(self, refresh: bool = False) -> Path | None:
        """Ensure the current archive is present and return its local path.

        A current-day, size-validated sidecar avoids all network calls. On other online runs one
        HEAD identifies the daily generation; a matching file then avoids the 1.39 GB GET.
        """
        if self._ensured and not refresh:
            return self._ensured_path
        if refresh:
            self.close()

        if not refresh:
            fast = self._same_day_fast_path()
            if fast is not None:
                self._ensured = True
                self._ensured_path = fast
                self._remove_old_generations(fast)
                return fast

        if bool(getattr(self.client, "offline", False)):
            cached = self._offline_cache()
            self._ensured = True
            self._ensured_path = cached
            return cached

        headers = self.client.head(self.URL)
        if headers is None:
            cached = self._offline_cache()
            self._ensured = True
            self._ensured_path = cached
            return cached

        last_modified = _header(headers, "Last-Modified")
        generation, _ = self._last_modified(last_modified)
        expected_bytes = _positive_int(_header(headers, "Content-Length"), "Content-Length")
        destination = self.cache_dir / f"companyfacts_{generation}.zip"

        if not refresh and destination.is_file() and destination.stat().st_size == expected_bytes:
            # A size match is not an integrity match: _read_sidecar re-verifies the
            # recorded digest, and a missing sidecar is rebuilt from freshly hashed bytes.
            sidecar = self._read_sidecar(destination)
            if sidecar is None:
                digest, member_count = self._archive_metadata(destination)
                self._write_sidecar(
                    destination,
                    last_modified=last_modified,
                    digest=digest,
                    member_count=member_count,
                )
                self._verified_digests[destination] = digest
            self._remove_old_generations(destination)
            self._ensured = True
            self._ensured_path = destination
            return destination

        get_with_headers = getattr(self.client, "get_bytes_with_headers", None)
        if callable(get_with_headers):
            response = get_with_headers(self.URL)
            if response is None:
                content = None
                representation_headers = headers
            else:
                content, representation_headers = response
        else:
            # Production SECClient exposes the actual GET headers. Retain compatibility for
            # small injected clients while keeping the network path generation-safe.
            content = self.client.get_bytes(self.URL)
            representation_headers = headers
        if content is None:
            cached = self._offline_cache()
            self._ensured = True
            self._ensured_path = cached
            return cached

        representation_last_modified = _header(representation_headers, "Last-Modified")
        representation_generation, _ = self._last_modified(representation_last_modified)
        representation_bytes = _positive_int(
            _header(representation_headers, "Content-Length"),
            "GET Content-Length",
        )
        if len(content) != representation_bytes:
            raise SECBulkFactsError(
                "SEC Companyfacts download length mismatch: "
                f"GET advertised {representation_bytes}, received {len(content)}"
            )
        destination = self.cache_dir / f"companyfacts_{representation_generation}.zip"
        if (
            representation_bytes != expected_bytes
            or representation_last_modified != last_modified
        ):
            log.warning(
                "SEC Companyfacts HEAD and GET described different generations; "
                "validating and caching the GET representation"
            )

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".zip.tmp",
                prefix=f".{destination.name}.",
                dir=self.cache_dir,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            digest, member_count = self._archive_metadata(temporary)
            temporary.replace(destination)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

        self._write_sidecar(
            destination,
            last_modified=representation_last_modified,
            digest=digest,
            member_count=member_count,
        )
        self._remove_old_generations(destination)
        self._ensured = True
        self._ensured_path = destination
        return destination

    def provenance(self) -> dict[str, Any] | None:
        """Return a copy of the active sidecar provenance record."""
        path = self.ensure()
        if path is None:
            return None
        payload = self._read_sidecar(path)
        if payload is None:
            return None
        return {key: value for key, value in payload.items() if not key.startswith("_")}

    def _open(self, path: Path) -> zipfile.ZipFile:
        if self._archive is not None and self._archive_path == path:
            return self._archive
        if self._archive is not None:
            self._archive.close()
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise SECBulkFactsError(f"invalid SEC Companyfacts ZIP: {path}") from exc
        self._archive = archive
        self._archive_path = path
        return archive

    def company_facts(self, cik: str) -> dict[str, Any] | None:
        """Return one ``CIK##########.json`` member, or ``None`` when it is absent."""
        identifier = str(cik).strip()
        if not identifier.isdigit() or len(identifier) > 10:
            raise ValueError(f"invalid SEC CIK: {cik!r}")
        path = self.ensure()
        if path is None:
            return None
        member = f"CIK{identifier.zfill(10)}.json"
        try:
            content = self._open(path).read(member)
        except KeyError:
            return None
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SECBulkFactsError(f"{member} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SECBulkFactsError(f"{member} is not a JSON object")
        return payload

    def close(self) -> None:
        if self._archive is not None:
            self._archive.close()
            self._archive = None
            self._archive_path = None
