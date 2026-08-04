"""FoundryProvider: consume Stock-Data foundry artifacts via their manifest contract.

The ecosystem integrates through published datasets, never code imports (see
ECOSYSTEM.md, linked from AGENTS.md). Each foundry dataset directory carries a
``manifest.json`` with a ``schema_version``, source URLs, a license note, and
sha256 hashes for every file. This adapter enforces that contract: unknown
schema versions are refused and hashes are verified before any row is trusted.

Two access modes, chosen by constructor argument:
- ``root``: a local clone/checkout of the Stock-Data repository.
- ``url_base``: raw.githubusercontent-style base URL for the repository
  (public repo, no auth), e.g.
  ``https://raw.githubusercontent.com/TylerJForstrom/Stock-Data/main``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from .symbols import ticker_variants

log = logging.getLogger(__name__)

__all__ = ["FoundryDataSource", "FoundryError"]

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})

_LISTED_EXCHANGES = frozenset({"NYSE", "Nasdaq", "NYSE American", "NYSE Arca", "CBOE", "BATS"})
_SYMBOL_DIRECTORY_FILES = frozenset(
    {
        "nasdaqlisted.jsonl",
        "otherlisted.jsonl",
        "sec_company_tickers_exchange.jsonl",
    }
)
# The event stream tags each diff with the directory it came from. Replaying a
# directory point-in-time needs that tag, so map file name -> event source.
_SYMBOL_DIRECTORY_EVENT_SOURCE = {
    "nasdaqlisted.jsonl": "nasdaqlisted",
    "otherlisted.jsonl": "otherlisted",
    "sec_company_tickers_exchange.jsonl": "sec_company_tickers_exchange",
}
# TickerPulse attention/sentiment aggregates, mirrored into the foundry daily
# (ECOSYSTEM rule: TickerPulse metrics enter the grader through the foundry,
# never directly). Public, allowlist-reviewed counts and scores only — the
# mirror carries no post text, author identity, or post identifiers.
_SENTIMENT_DATASET_DIRS = {
    "ticker_trends": "data/sentiment/ticker_trends",
    "ticker_buckets": "data/sentiment/ticker_buckets",
}


class FoundryError(RuntimeError):
    """Contract violation: missing dataset, unknown schema, or hash mismatch."""


class FoundryDataSource:
    """Read-only view over the foundry's published datasets."""

    def __init__(
        self,
        root: str | Path | None = None,
        url_base: str | None = None,
        *,
        verify_hashes: bool = True,
        timeout: float = 60.0,
    ) -> None:
        if (root is None) == (url_base is None):
            raise ValueError("provide exactly one of root (local clone) or url_base (raw URL)")
        self.root = Path(root).resolve() if root is not None else None
        self.url_base = url_base.rstrip("/") if url_base is not None else None
        self.verify_hashes = verify_hashes
        self.timeout = timeout
        self._dividends_frame: pd.DataFrame | None = None

    # -- transport ---------------------------------------------------------

    def _read_bytes(self, relpath: str) -> bytes:
        if self.root is not None:
            path = (self.root / relpath).resolve()
            if self.root not in path.parents and path != self.root:
                raise FoundryError(f"path escapes foundry root: {relpath}")
            try:
                return path.read_bytes()
            except OSError as exc:
                raise FoundryError(f"missing foundry file: {relpath}") from exc
        import requests

        url = f"{self.url_base}/{relpath}"
        try:
            response = requests.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": "Stock-Grader foundry adapter"},
            )
        except requests.RequestException as exc:
            raise FoundryError(f"foundry fetch failed: {url}") from exc
        if response.status_code != 200:
            raise FoundryError(f"foundry fetch HTTP {response.status_code}: {url}")
        return response.content

    # -- contract ----------------------------------------------------------

    def manifest(self, dataset_dir: str) -> dict[str, Any]:
        """Load and validate a dataset's manifest. Refuses unknown schemas."""
        payload = json.loads(self._read_bytes(f"{dataset_dir}/manifest.json"))
        version = payload.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise FoundryError(
                f"unknown foundry schema_version {version!r} in {dataset_dir} "
                f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}); refusing to read"
            )
        return payload

    def _read_dataset_file(self, dataset_dir: str, name: str) -> bytes:
        """Read a file listed in its dataset manifest, verifying its sha256."""
        manifest = self.manifest(dataset_dir)
        entry = next((f for f in manifest.get("files", []) if f.get("name") == name), None)
        if entry is None:
            raise FoundryError(f"{name} is not listed in {dataset_dir}/manifest.json")
        blob = self._read_bytes(f"{dataset_dir}/{name}")
        if self.verify_hashes:
            digest = hashlib.sha256(blob).hexdigest()
            if digest != entry.get("sha256"):
                raise FoundryError(
                    f"sha256 mismatch for {dataset_dir}/{name}: manifest says "
                    f"{entry.get('sha256')!r}, file hashes to {digest!r}"
                )
        return blob

    # -- datasets ----------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        """Listing/delisting/change events from the daily diff stream."""
        blob = self._read_dataset_file("data/symbols/events", "events.jsonl")
        return [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]

    def _assert_reconstructable(self, stream: list[dict[str, Any]], asof: str) -> None:
        """Refuse an asof the event archive cannot testify about.

        An event dated D proves snapshots existed on D-1 and D, so the earliest
        reconstructable membership date is earliest_event - 1. Before that the
        stream cannot testify: refuse rather than serve today's survivors dressed
        up as history.
        """
        earliest = min((str(e.get("date", "")) for e in stream), default=None)
        if earliest is None:
            return
        import datetime as _dt

        boundary = (_dt.date.fromisoformat(earliest) - _dt.timedelta(days=1)).isoformat()
        if asof < boundary:
            raise FoundryError(
                f"asof {asof} predates the event archive (reconstructable "
                f"from {boundary}); point-in-time membership unavailable"
            )

    def symbol_directory(self, name: str, *, asof: str | None = None) -> list[dict[str, Any]]:
        """Read one manifest-verified symbol directory used by universe screens.

        ``asof`` (ISO date) reconstructs the directory point-in-time by replaying
        the event stream BACKWARD from the current snapshot, exactly as
        :meth:`universe` does: rows added after asof are removed, rows removed
        after asof are restored, rows changed after asof revert to their recorded
        previous state.

        Without ``asof`` this returns TODAY's directory. Filtering a historical
        universe through today's listing directory silently drops every issuer
        delisted since — which is precisely the survivorship bias the
        point-in-time contract exists to eliminate — so any caller building a
        dated artifact must pass ``asof``.
        """
        if name not in _SYMBOL_DIRECTORY_FILES:
            raise ValueError(f"unsupported symbol directory: {name}")
        blob = self._read_dataset_file("data/symbols/current", name)
        records = [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]
        if asof is None:
            return records
        source_key = _SYMBOL_DIRECTORY_EVENT_SOURCE[name]
        stream = self.events()
        self._assert_reconstructable(stream, asof)
        # Directory rows are keyed by ticker alone; the SEC exchange directory
        # additionally carries a CIK, and universe() owns that (cik, ticker) form.
        members = {str(r.get("ticker", "")): r for r in records}
        replayed = [
            e for e in stream if e.get("source") == source_key and str(e.get("date", "")) > asof
        ]
        for event in sorted(replayed, key=lambda e: str(e.get("date", "")), reverse=True):
            record = event.get("record") or {}
            key = str(record.get("ticker", ""))
            kind = event.get("event")
            if kind == "added":
                members.pop(key, None)
            elif kind == "removed":
                members[key] = record
            elif kind == "changed" and event.get("previous"):
                members[key] = event["previous"]
        return list(members.values())

    def universe(
        self, *, listed_only: bool = True, asof: str | None = None
    ) -> list[dict[str, Any]]:
        """CIK/ticker/exchange records from the daily symbol snapshot.

        ``listed_only`` keeps records whose exchange is a listed venue —
        the peer-hygiene filter that keeps OTC/pink-sheet names out of
        cross-sectional grading.

        ``asof`` (ISO date) reconstructs point-in-time membership by replaying
        the event stream BACKWARD from the current snapshot: additions after
        asof are removed, removals after asof are restored, changes after asof
        revert to their recorded previous state. Tickers get reused after
        delistings — grading a past date against today's membership silently
        maps dead issuers onto their symbol's new owners. The archive only
        reaches back to its first snapshot (2026-07-28); earlier asof values
        raise rather than silently serving today's survivors.
        """
        blob = self._read_dataset_file("data/symbols/current", "sec_company_tickers_exchange.jsonl")
        records = [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]
        if asof is not None:
            members = {(r.get("cik"), r.get("ticker")): r for r in records}
            stream = self.events()
            self._assert_reconstructable(stream, asof)
            replayed = [
                e
                for e in stream
                if e.get("source") == "sec_company_tickers_exchange"
                and str(e.get("date", "")) > asof
            ]
            for event in sorted(replayed, key=lambda e: str(e.get("date", "")), reverse=True):
                record = event.get("record") or {}
                key = (record.get("cik"), record.get("ticker"))
                kind = event.get("event")
                if kind == "added":
                    members.pop(key, None)
                elif kind == "removed":
                    members[key] = record
                elif kind == "changed" and event.get("previous"):
                    members[key] = event["previous"]
            records = list(members.values())
        if listed_only:
            records = [r for r in records if r.get("exchange") in _LISTED_EXCHANGES]
        return records

    def universe_tickers(self, *, listed_only: bool = True, asof: str | None = None) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for record in self.universe(listed_only=listed_only, asof=asof):
            ticker = record.get("ticker")
            if ticker and ticker not in seen:
                seen.add(ticker)
                out.append(ticker)
        return out

    def _sentiment_dataset_dir(self, dataset: str) -> str:
        try:
            return _SENTIMENT_DATASET_DIRS[dataset]
        except KeyError:
            raise ValueError(
                f"unsupported sentiment dataset: {dataset!r} "
                f"(choices: {sorted(_SENTIMENT_DATASET_DIRS)})"
            ) from None

    def sentiment_days(self, dataset: str = "ticker_trends") -> list[str]:
        """ISO dates with a mirrored TickerPulse file, from the manifest.

        Only manifested days are listed — an unmanifested file on disk is
        unreadable by contract, so it is not advertised either.
        """
        import datetime as _dt

        manifest = self.manifest(self._sentiment_dataset_dir(dataset))
        days = []
        for entry in manifest.get("files", []):
            name = str(entry.get("name", ""))
            if not name.endswith(".jsonl"):
                continue
            stem = name[: -len(".jsonl")]
            try:
                _dt.date.fromisoformat(stem)
            except ValueError:
                continue
            days.append(stem)
        return sorted(days)

    def sentiment_trends(self, day: str) -> list[dict[str, Any]]:
        """Per-ticker daily attention/sentiment aggregates for one mirrored day.

        Rows carry the allowlist-reviewed TickerPulse fields (mentions,
        mentions_prev, engagement, bull/bear/neutral, sentiment_avg,
        breakout_score, velocity, share_of_voice, phase, ...) — aggregate
        counts and scores over public social posts; never post text or author
        identity.

        Point-in-time note for signal work: the file dated D summarizes a
        24-hour window ending in the early UTC hours of D (US evening of D-1)
        and is mirrored into the foundry mid-UTC-morning on D — knowable well
        before D's US market close. Any dated artifact built from it must
        treat D as the earliest usable signal date, never D-1.
        """
        blob = self._read_dataset_file(
            _SENTIMENT_DATASET_DIRS["ticker_trends"], f"{day}.jsonl"
        )
        return [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]

    def sentiment_buckets(self, day: str) -> list[dict[str, Any]]:
        """Hourly per-ticker mention/sentiment buckets for one mirrored day.

        Same producer and licensing as :meth:`sentiment_trends`; rows carry
        bucket_start (UTC) and bucket_minutes alongside the aggregate counts.
        """
        blob = self._read_dataset_file(
            _SENTIMENT_DATASET_DIRS["ticker_buckets"], f"{day}.jsonl"
        )
        return [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]

    def dividends(self) -> pd.DataFrame:
        """Reconstructed dividends-per-share on the current split basis.

        Columns include ticker, period_start, period_end, span_type,
        dps_current_basis, derived, approximate, flags. Fiscal-period
        granularity — no ex-dates (the foundry manifests state this limit).

        Read, hash-verified, and parsed once per instance; every call returns
        the same cached frame. :meth:`trailing_dps` runs once per ticker across
        a whole universe, so re-verifying the parquet on each call would turn
        one dataset read into thousands. Callers must treat the frame as
        read-only.
        """
        if self._dividends_frame is None:
            blob = self._read_dataset_file("data/corporate_actions", "dividends.parquet")
            self._dividends_frame = pd.read_parquet(io.BytesIO(blob))
        return self._dividends_frame

    def splits(self) -> pd.DataFrame:
        """Split events: ticker, effective_date, ratio (post/pre), confidence."""
        blob = self._read_dataset_file("data/corporate_actions", "splits.jsonl")
        rows = [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["effective_date"] = pd.to_datetime(frame["effective_date"])
        return frame

    def trailing_dps(self, ticker: str, *, asof: pd.Timestamp | None = None) -> float | None:
        """Trailing ~12-month dividends per share for a ticker, current basis.

        Sums quarterly/monthly records whose period_end falls in the 370 days
        before ``asof`` (default: newest period_end for the ticker). Returns
        None when the foundry has no usable rows — callers must treat that as
        unknown, not zero.

        Matches every ticker spelling (the foundry stores the SEC dash form;
        callers arrive with dot or space forms), so ``BRK.B`` finds ``BRK-B``
        rows instead of silently reporting an unknown dividend.
        """
        table = self.dividends()
        rows = table[
            table["ticker"].isin(ticker_variants(ticker))
            & table["span_type"].isin(("quarterly", "monthly"))
        ]
        if rows.empty:
            return None
        ends = pd.to_datetime(rows["period_end"])
        cutoff = asof if asof is not None else ends.max()
        window = rows[(ends <= cutoff) & (ends > cutoff - pd.Timedelta(days=370))]
        if window.empty:
            return None
        return float(window["dps_current_basis"].sum())
