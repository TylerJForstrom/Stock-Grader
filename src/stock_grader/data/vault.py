"""VaultDataSource: consume Stock-Vault's private archives (local clone only).

The vault collects what no live provider can serve — survivorship-proof
whole-market EOD bars, borrow fees, and price histories of delisted
companies — but until this adapter existed it was write-only. Access is
local-clone-only by design: the repo is private and its license notes
prohibit anything that would put raw vendor data behind a URL.

Same manifest discipline as the public foundry: files must be listed in
their dataset's manifest.json (schema_version checked) and sha256-verified
before a row is trusted.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
from pathlib import Path

import pandas as pd

from .symbols import ticker_variants

log = logging.getLogger(__name__)

__all__ = ["VaultDataSource", "VaultError", "VaultPriceProvider"]

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})


class VaultError(RuntimeError):
    """Missing dataset, unknown schema, or hash mismatch in the vault."""


class VaultDataSource:
    """Read-only, hash-verified view over a local Stock-Vault clone."""

    def __init__(self, root: str | Path, *, verify_hashes: bool = True) -> None:
        self.root = Path(root).resolve()
        if not (self.root / "data").is_dir():
            raise VaultError(f"{self.root} does not look like a Stock-Vault clone (no data/)")
        self.verify_hashes = verify_hashes
        self._manifest_cache: dict[str, dict] = {}

    # -- contract ----------------------------------------------------------

    def _manifest(self, dataset_dir: str) -> dict:
        if dataset_dir in self._manifest_cache:
            return self._manifest_cache[dataset_dir]
        path = self.root / dataset_dir / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise VaultError(f"missing manifest for {dataset_dir}") from exc
        if payload.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            raise VaultError(
                f"unknown vault schema_version {payload.get('schema_version')!r} "
                f"in {dataset_dir}; refusing to read"
            )
        self._manifest_cache[dataset_dir] = payload
        return payload

    def _read_verified(self, dataset_dir: str, name: str) -> bytes:
        manifest = self._manifest(dataset_dir)
        entry = next((f for f in manifest.get("files", []) if f.get("name") == name), None)
        if entry is None:
            raise VaultError(f"{name} is not listed in {dataset_dir}/manifest.json")
        blob = (self.root / dataset_dir / name).read_bytes()
        if self.verify_hashes:
            digest = hashlib.sha256(blob).hexdigest()
            if digest != entry.get("sha256"):
                raise VaultError(f"sha256 mismatch for {dataset_dir}/{name}")
        return blob

    @staticmethod
    def _jsonl_gz(blob: bytes) -> list[dict]:
        text = gzip.decompress(blob).decode("utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    # -- whole-market EOD --------------------------------------------------

    def market_eod_day(self, day: dt.date) -> pd.DataFrame:
        """Every US ticker's OHLCV bar for one date. Raises on non-archived days."""
        dataset = f"data/market_eod/{day.strftime('%Y-%m')}"
        rows = self._jsonl_gz(self._read_verified(dataset, f"{day.isoformat()}.jsonl.gz"))
        return pd.DataFrame(rows)

    def market_eod_available_days(self) -> list[dt.date]:
        days = []
        eod_root = self.root / "data" / "market_eod"
        if not eod_root.is_dir():
            return []
        for month_dir in sorted(eod_root.iterdir()):
            if not month_dir.is_dir():
                continue
            for file in sorted(month_dir.glob("*.jsonl.gz")):
                try:
                    days.append(dt.date.fromisoformat(file.name.removesuffix(".jsonl.gz")))
                except ValueError:
                    continue
        return days

    def market_eod_series(
        self, ticker: str, *, start: dt.date | None = None, end: dt.date | None = None
    ) -> pd.DataFrame | None:
        """One ticker's daily bars assembled from the day files.

        Matches all ticker spellings (Polygon uses dot class notation, SEC uses
        dashes). Works for delisted names too — that is the point.
        """
        wanted = set(ticker_variants(ticker))
        frames = []
        for day in self.market_eod_available_days():
            if (start and day < start) or (end and day > end):
                continue
            table = self.market_eod_day(day)
            if table.empty:
                continue
            hit = table[table["symbol"].astype(str).str.upper().isin(wanted)]
            if not hit.empty:
                row = hit.iloc[0].to_dict()
                row["date"] = day
                frames.append(row)
        if not frames:
            return None
        out = pd.DataFrame(frames).set_index("date").sort_index()
        out.index = pd.to_datetime(out.index)
        return out

    # -- borrow ------------------------------------------------------------

    def borrow_latest(self) -> pd.DataFrame:
        """Most recent shortable-stock snapshot (fees, availability)."""
        borrow_root = self.root / "data" / "borrow"
        newest: tuple[str, str] | None = None
        for month_dir in sorted(borrow_root.iterdir(), reverse=True):
            if not month_dir.is_dir():
                continue
            snapshots = sorted(month_dir.glob("usa_*.jsonl.gz"), reverse=True)
            if snapshots:
                newest = (f"data/borrow/{month_dir.name}", snapshots[0].name)
                break
        if newest is None:
            raise VaultError("no borrow snapshots in the vault")
        rows = self._jsonl_gz(self._read_verified(*newest))
        return pd.DataFrame(rows)

    def borrow_fee(self, ticker: str) -> dict | None:
        """Latest fee/availability row for one ticker (IB uses space notation)."""
        table = self.borrow_latest()
        wanted = {v.replace("-", " ").replace(".", " ") for v in ticker_variants(ticker)}
        wanted |= set(ticker_variants(ticker))
        hit = table[table["symbol"].astype(str).str.upper().isin(wanted)]
        return hit.iloc[0].to_dict() if not hit.empty else None

    # -- delisted prices ---------------------------------------------------

    def delisted_history(self, symbol: str) -> pd.DataFrame | None:
        """Price history of a dead company from the cohort archives."""
        wanted = set(ticker_variants(symbol))
        delisted_root = self.root / "data" / "delisted_prices"
        if not delisted_root.is_dir():
            return None
        for year_dir in sorted(delisted_root.iterdir(), reverse=True):
            if not year_dir.is_dir():
                continue
            for candidate in wanted:
                path = year_dir / f"{candidate}.json.gz"
                if not path.exists():
                    continue
                payload = json.loads(
                    gzip.decompress(
                        self._read_verified(
                            f"data/delisted_prices/{year_dir.name}", path.name
                        )
                    )
                )
                data = payload.get("data", payload)
                if isinstance(data, dict):
                    data = data.get("data", data)
                frame = pd.DataFrame(data)
                if frame.empty:
                    return None
                if "t" in frame.columns:
                    frame["date"] = pd.to_datetime(frame["t"], unit="s", errors="coerce")
                    if frame["date"].isna().all():
                        frame["date"] = pd.to_datetime(frame["t"], errors="coerce")
                    frame = frame.set_index("date")
                return frame
        return None


class VaultPriceProvider:
    """PriceProvider adapter: serve grader price frames from the vault archive.

    Unadjusted closes (collected with adjusted=false) — downstream adjustment
    tracking flags them honestly. Serves live AND dead tickers, needs no
    network, and has no rate limit, which makes it the preferred historical
    source once the archive covers the requested window.
    """

    name = "vault"
    provides_prices = True

    def __init__(self, vault: VaultDataSource) -> None:
        self.vault = vault

    def _fetch(self, ticker: str, *, start=None, end=None) -> pd.DataFrame | None:
        series = self.vault.market_eod_series(
            ticker,
            start=start if isinstance(start, dt.date) else None,
            end=end if isinstance(end, dt.date) else None,
        )
        if series is None or series.empty:
            return None
        frame = series.rename(
            columns={"open": "open", "high": "high", "low": "low",
                     "close": "close", "volume": "volume"}
        )
        return frame[[c for c in ("open", "high", "low", "close", "volume") if c in frame]]
