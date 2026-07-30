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
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from .cache import default_cache_dir
from .prices import PriceProvider
from .symbols import ticker_variants

log = logging.getLogger(__name__)

__all__ = ["VaultDataSource", "VaultError", "VaultPriceProvider"]

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})
_MARKET_EOD_PANEL_SCHEMA_VERSION = 1
_MARKET_EOD_COLUMNS = (
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "transactions",
)
_MARKET_EOD_NUMERIC = ("open", "high", "low", "close", "volume", "vwap", "transactions")


class VaultError(RuntimeError):
    """Missing dataset, unknown schema, or hash mismatch in the vault."""


def _atomic_parquet_write(frame: pd.DataFrame, destination: Path) -> None:
    temporary: Path | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".parquet.tmp",
            prefix=f".{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        frame.to_parquet(temporary, index=False)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_market_eod_panel(frame: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(set(_MARKET_EOD_COLUMNS) - set(frame.columns))
    if missing:
        raise VaultError("market_eod rows are missing required fields: " + ", ".join(missing))
    table = frame.loc[:, _MARKET_EOD_COLUMNS].copy()
    table["date"] = pd.to_datetime(table["date"], errors="raise").dt.normalize()
    table["symbol"] = table["symbol"].astype(str).str.upper().str.strip()
    if table["symbol"].eq("").any():
        raise VaultError("market_eod contains an empty symbol")
    for column in _MARKET_EOD_NUMERIC:
        if table[column].map(lambda value: isinstance(value, (bool, np.bool_))).any():
            raise VaultError(f"market_eod {column} contains an unparseable numeric value")
        try:
            table[column] = pd.to_numeric(table[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise VaultError(f"market_eod {column} contains an unparseable numeric value") from exc
        if not np.isfinite(table[column].to_numpy(dtype="float64")).all():
            raise VaultError(f"market_eod {column} contains a non-finite numeric value")
        if table[column].isna().any():
            raise VaultError(f"market_eod {column} contains a missing numeric value")
    return table.sort_values(["date", "symbol"]).reset_index(drop=True)


class VaultDataSource:
    """Read-only, hash-verified view over a local Stock-Vault clone."""

    def __init__(self, root: str | Path, *, verify_hashes: bool = True) -> None:
        self.root = Path(root).resolve()
        if not (self.root / "data").is_dir():
            raise VaultError(f"{self.root} does not look like a Stock-Vault clone (no data/)")
        self.verify_hashes = verify_hashes
        self._manifest_cache: dict[str, dict] = {}

        self._market_eod_panels: dict[
            tuple[dt.date | None, dt.date | None, Path], pd.DataFrame
        ] = {}

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

    def market_eod_panel(
        self,
        start: dt.date | None = None,
        end: dt.date | None = None,
        cache_dir: str | Path | None = None,
    ) -> pd.DataFrame:
        """All archived OHLCV rows in one long frame, reading each day file once."""
        if start is not None and end is not None and start > end:
            raise ValueError("market_eod panel start cannot be after end")
        days = [
            day
            for day in self.market_eod_available_days()
            if (start is None or day >= start) and (end is None or day <= end)
        ]
        if not days:
            return pd.DataFrame(columns=_MARKET_EOD_COLUMNS)

        cache_root = Path(cache_dir or default_cache_dir("vault")).resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        key = (start, end, cache_root)
        if key in self._market_eod_panels:
            return self._market_eod_panels[key]
        destination = cache_root / (
            f"market_eod_{days[0].isoformat()}_{days[-1].isoformat()}"
            f"_v{_MARKET_EOD_PANEL_SCHEMA_VERSION}.parquet"
        )
        if destination.is_file():
            try:
                cached = _validate_market_eod_panel(pd.read_parquet(destination))
            except (OSError, ValueError, VaultError):
                log.warning("invalid vault market_eod panel cache %s; rebuilding", destination)
            else:
                self._market_eod_panels[key] = cached
                return cached

        frames: list[pd.DataFrame] = []
        for day in days:
            table = self.market_eod_day(day)
            if table.empty:
                continue
            table = table.copy()
            table.insert(0, "date", pd.Timestamp(day))
            frames.append(table)
        if not frames:
            return pd.DataFrame(columns=_MARKET_EOD_COLUMNS)
        panel = _validate_market_eod_panel(pd.concat(frames, ignore_index=True))
        _atomic_parquet_write(panel, destination)
        self._market_eod_panels[key] = panel
        return panel

    @staticmethod
    def _series_from_panel(panel: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
        wanted = set(ticker_variants(ticker))
        hits = panel[panel["symbol"].isin(wanted)]
        if hits.empty:
            return None
        return hits.set_index("date").sort_index()

    def market_eod_series(
        self, ticker: str, *, start: dt.date | None = None, end: dt.date | None = None
    ) -> pd.DataFrame | None:
        """One ticker's daily bars assembled from the day files.

        Matches all ticker spellings (Polygon uses dot class notation, SEC uses
        dashes). Works for delisted names too — that is the point.
        """
        panel = self.market_eod_panel(start=start, end=end)
        return self._series_from_panel(panel, ticker)

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
                        self._read_verified(f"data/delisted_prices/{year_dir.name}", path.name)
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


class VaultPriceProvider(PriceProvider):
    """PriceProvider adapter: serve grader price frames from the vault archive.

    Unadjusted closes (collected with adjusted=false) — downstream adjustment
    tracking flags them honestly. Serves live AND dead tickers, needs no
    network, and has no rate limit, which makes it the preferred historical
    source once the archive covers the requested window.
    """

    name = "vault"
    provides_prices = True

    def __init__(self, vault: VaultDataSource, *, cache_dir: str | Path | None = None) -> None:
        self.vault = vault
        self.cache_dir = cache_dir
        self._panel: pd.DataFrame | None = None
        self._indexed: pd.DataFrame | None = None
        self._symbols: frozenset[str] | None = None

    def _ensure_panel(self) -> None:
        """Validate the feed before PriceProvider's fallback boundary can hide an error."""
        if self._indexed is None:
            self._panel = self.vault.market_eod_panel(cache_dir=self.cache_dir)
            indexed = self._panel.copy()
            indexed["symbol"] = indexed["symbol"].astype(str).str.upper()
            self._indexed = indexed.set_index(["symbol", "date"]).sort_index()
            self._symbols = frozenset(self._indexed.index.get_level_values("symbol"))

    def get(
        self,
        ticker: str,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pd.DataFrame | None:
        # The generic provider intentionally turns transport failures into a chain fallback. A
        # malformed local numeric archive is different: silently falling through would mix
        # providers and make the run look valid, so validation must happen outside that boundary.
        self._ensure_panel()
        return super().get(ticker, start=start, end=end)

    def _fetch(self, ticker: str, *, start=None, end=None) -> pd.DataFrame | None:
        self._ensure_panel()
        assert self._indexed is not None
        assert self._symbols is not None
        if self._indexed.empty:
            return None
        frames = [
            self._indexed.xs(candidate, level="symbol").copy()
            for candidate in ticker_variants(ticker)
            if candidate in self._symbols
        ]
        if not frames:
            return None
        series = pd.concat(frames).sort_index()
        series = series[~series.index.duplicated(keep="first")]
        if start is not None:
            series = series[series.index >= pd.Timestamp(start)]
        if end is not None:
            series = series[series.index <= pd.Timestamp(end)]
        columns = ("open", "high", "low", "close", "volume")
        return series[[column for column in columns if column in series]]
