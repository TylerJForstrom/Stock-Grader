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

log = logging.getLogger(__name__)

__all__ = ["FoundryDataSource", "FoundryError"]

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})

_LISTED_EXCHANGES = frozenset({"NYSE", "Nasdaq", "NYSE American", "NYSE Arca", "CBOE", "BATS"})


class FoundryError(RuntimeError):
    """Contract violation: missing dataset, unknown schema, or hash mismatch."""


class FoundryDataSource:
    """Read-only view over the foundry's published datasets."""

    def __init__(self, root: str | Path | None = None, url_base: str | None = None,
                 *, verify_hashes: bool = True, timeout: float = 60.0) -> None:
        if (root is None) == (url_base is None):
            raise ValueError("provide exactly one of root (local clone) or url_base (raw URL)")
        self.root = Path(root).resolve() if root is not None else None
        self.url_base = url_base.rstrip("/") if url_base is not None else None
        self.verify_hashes = verify_hashes
        self.timeout = timeout

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
                url, timeout=self.timeout,
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

    def universe(self, *, listed_only: bool = True) -> list[dict[str, Any]]:
        """CIK/ticker/exchange records from the daily symbol snapshot.

        ``listed_only`` keeps records whose exchange is a listed venue —
        the peer-hygiene filter that keeps OTC/pink-sheet names out of
        cross-sectional grading.
        """
        blob = self._read_dataset_file(
            "data/symbols/current", "sec_company_tickers_exchange.jsonl"
        )
        records = [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]
        if listed_only:
            records = [r for r in records if r.get("exchange") in _LISTED_EXCHANGES]
        return records

    def universe_tickers(self, *, listed_only: bool = True) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for record in self.universe(listed_only=listed_only):
            ticker = record.get("ticker")
            if ticker and ticker not in seen:
                seen.add(ticker)
                out.append(ticker)
        return out

    def dividends(self) -> pd.DataFrame:
        """Reconstructed dividends-per-share on the current split basis.

        Columns include ticker, period_start, period_end, span_type,
        dps_current_basis, derived, approximate, flags. Fiscal-period
        granularity — no ex-dates (the foundry manifests state this limit).
        """
        blob = self._read_dataset_file("data/corporate_actions", "dividends.parquet")
        return pd.read_parquet(io.BytesIO(blob))

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
        """
        table = self.dividends()
        rows = table[
            table["ticker"].eq(ticker.upper())
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
