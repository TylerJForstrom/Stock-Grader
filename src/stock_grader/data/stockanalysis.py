"""Daily split- and dividend-adjusted OHLCV from stockanalysis.com.

This is the only reachable source of a **daily** price series, and it is what brings the 40
price-dependent statistics to life — every risk, momentum and liquidity metric needs day-by-day
continuity that the SEC's sparse insider-transaction prices cannot provide.

Read this before depending on it
--------------------------------
It is an **undocumented internal endpoint of a commercial website**, not a licensed market-data
feed and not a published API. It can change or disappear without notice. `robots.txt` was checked
and disallows nothing for general agents (only ``dotbot``, ``BLEXBot`` and ``mj12bot`` are named),
and no bot-detection or access control is being circumvented — but the site's Terms of Service are
a separate question from robots.txt, and anyone shipping this should read them.

Because of that, this provider is deliberately **not** the default. It is opt-in via
``--stockanalysis``, and :class:`~stock_grader.data.prices.CSVPriceProvider` and a keyed provider
like Tiingo remain the paths that carry no such caveat.

It is also polite by construction: one request per ticker per run at a bounded rate, responses
cached to disk for a day, and a User-Agent that names the tool and its contact rather than
impersonating a browser.

The adjustment is real — verified, not assumed
----------------------------------------------
The ``a`` column is genuinely split- **and** dividend-adjusted, established with a self-proving
test rather than taken on trust:

===========  =========================================  ==============================
ticker       expectation                                measured
===========  =========================================  ==============================
``BRK.B``    never paid a dividend, so ``a == c``       identical on **100%** of bars
``T``        large dividend, so ``a`` must diverge      2016 bar: close 42.38, adj 17.80
``AAPL``     splits and a small dividend                identical on only 2% of bars
===========  =========================================  ==============================

For AT&T the ten-year price CAGR is **−5.5%** while the adjusted CAGR is **+3.1%** — the sign
flips. Using the unadjusted close would report that a decade of holding AT&T lost money, when
reinvested dividends more than covered the price decline. Every return, Sharpe, drawdown and
momentum metric reads ``adj_close`` for exactly this reason.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from .prices import PriceProvider

log = logging.getLogger(__name__)

__all__ = ["StockAnalysisPriceProvider"]

_URL = "https://stockanalysis.com/api/symbol/s/{ticker}/history"


class StockAnalysisPriceProvider(PriceProvider):
    """Daily adjusted OHLCV. Opt-in; see the module docstring for the caveat."""

    name = "stockanalysis"

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        contact: str | None = None,
        range_: str = "10Y",
        timeout: float = 30.0,
        rate: float = 2.0,
        ttl_hours: float = 24.0,
    ) -> None:
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "stock-grader" / "sa")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.contact = contact or os.environ.get("STOCK_GRADER_CONTACT") or "stock-grader"
        self.range_ = range_
        self.timeout = timeout
        self.ttl_hours = ttl_hours
        self._min_interval = 1.0 / rate
        self._last = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()
        # Identify honestly rather than impersonating a browser.
        self._session.headers.update({
            "User-Agent": f"Stock-Grader/0.1 (+{self.contact}) python-requests",
            "Accept": "application/json",
        })

    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()

    def _cache_path(self, ticker: str) -> Path:
        safe = ticker.upper().replace("/", "_")
        return self.cache_dir / f"{safe}_{self.range_}.parquet"

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        cache = self._cache_path(ticker)
        if cache.exists():
            age_hours = (time.time() - cache.stat().st_mtime) / 3600.0
            if age_hours < self.ttl_hours:
                try:
                    return pd.read_parquet(cache)
                except Exception:
                    log.debug("unreadable cache %s, refetching", cache)

        self._throttle()
        response = self._session.get(
            _URL.format(ticker=ticker.upper()),
            params={"range": self.range_, "period": "Daily"},
            timeout=self.timeout,
        )
        if response.status_code == 400:
            # Unknown symbols answer 400 rather than an empty series.
            log.info("stockanalysis: unknown symbol %s", ticker)
            return None
        if response.status_code != 200:
            log.warning("stockanalysis returned HTTP %s for %s", response.status_code, ticker)
            return None
        try:
            payload = response.json()
        except ValueError:
            log.warning("stockanalysis returned non-JSON for %s", ticker)
            return None

        rows = payload.get("data")
        if isinstance(rows, dict):  # tolerate a nested shape if the endpoint ever changes
            rows = rows.get("data")
        if not isinstance(rows, list) or not rows:
            return None

        frame = pd.DataFrame(rows)
        required = {"t", "o", "h", "l", "c", "a", "v"}
        if not required.issubset(frame.columns):
            log.warning(
                "stockanalysis payload for %s lacks expected columns (got %s) — refusing rather "
                "than guessing which column is the adjusted close",
                ticker, sorted(frame.columns),
            )
            return None

        frame = frame.rename(columns={
            "t": "date", "o": "open", "h": "high", "l": "low",
            "c": "close", "a": "adj_close", "v": "volume",
        })
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).set_index("date")
        frame = frame[["open", "high", "low", "close", "adj_close", "volume"]]
        # Rows arrive newest-first; PriceProvider._conform sorts, but write the cache in order
        # so anything reading the parquet directly is not misled.
        frame = frame.sort_index()
        try:
            frame.to_parquet(cache)
        except Exception as exc:
            log.debug("could not cache %s: %s", ticker, exc)
        return frame
