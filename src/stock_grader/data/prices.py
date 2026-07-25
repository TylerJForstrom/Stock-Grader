"""Price providers.

No free price feed was reachable from the build environment (Yahoo: HTTP 429 on every attempt with
browser headers, a cookie jar and retries; Stooq: a JavaScript proof-of-work bot check instead of
CSV). The Yahoo block is egress-level on a shared address and will very likely not apply on an
ordinary home network, so the provider ships anyway — it simply could not be verified here.

Consequences baked into this module:

* Every provider **fails soft**: a warning and ``None``, never an exception. A missing price feed
  degrades the risk and momentum pillars; it must not take down the grade.
* :class:`CSVPriceProvider` and the ``--price`` scalar override exist so a user with no market-data
  access at all still gets every valuation metric from a number they type in.
* :class:`ChainedPriceProvider` tries sources in order, which is how a keyed provider slots in
  ahead of the free ones without touching call sites.
"""

from __future__ import annotations

import io
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import ClassVar

import pandas as pd
import requests

log = logging.getLogger(__name__)

__all__ = [
    "OHLCV_COLUMNS",
    "BenchmarkProvider",
    "CSVPriceProvider",
    "ChainedPriceProvider",
    "PriceProvider",
    "RiskFreeProvider",
    "StooqPriceProvider",
    "TiingoPriceProvider",
    "YahooPriceProvider",
]

OHLCV_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _cache_fresh(path: Path, ttl_hours: float) -> bool:
    """Whether a cache file is younger than its time to live."""
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0 < ttl_hours
    except OSError:
        return False


def _conform(df: pd.DataFrame) -> pd.DataFrame:
    """Force any provider's output into the canonical schema so metrics never see quirks."""
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    if "adj_close" not in df.columns:
        for alt in ("adjclose", "adj._close", "adjusted_close"):
            if alt in df.columns:
                df["adj_close"] = df[alt]
                break
        else:
            if "close" in df.columns:
                df["adj_close"] = df["close"]
    for col in OHLCV_COLUMNS:
        if col not in df.columns:
            df[col] = float("nan")
    df = df[OHLCV_COLUMNS]
    df.index = pd.to_datetime(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Coerce rather than cast: providers ship strings, nulls and the occasional "N/A", and pd.NA
    # is not castable to float64. A bad cell becomes NaN instead of taking down the fetch.
    return df.apply(pd.to_numeric, errors="coerce").astype("float64")


class PriceProvider:
    """Base class. Subclasses override :meth:`_fetch` and never raise out of :meth:`get`."""

    name = "base"

    def get(self, ticker: str, *, start: date | None = None, end: date | None = None) -> pd.DataFrame | None:
        try:
            df = self._fetch(ticker, start=start, end=end)
            if df is None or df.empty:
                return None
            # _conform sat outside this block, so one unparseable date in one CSV raised straight
            # through a method documented as never raising — aborting the whole universe run.
            df = _conform(df)
            if start is not None:
                df = df[df.index >= pd.Timestamp(start)]
            if end is not None:
                df = df[df.index <= pd.Timestamp(end)]
        except Exception as exc:
            log.warning("%s price fetch failed for %s: %s", self.name, ticker, exc)
            return None
        return df if not df.empty else None

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        raise NotImplementedError


class CSVPriceProvider(PriceProvider):
    """Read prices from local CSVs — the reliable path when no API is reachable.

    Looks for ``<dir>/<TICKER>.csv`` with a date index and any subset of the OHLCV columns. A file
    with nothing but ``date,close`` is enough for every return, risk and momentum metric.
    """

    name = "csv"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        for candidate in (f"{ticker.upper()}.csv", f"{ticker.lower()}.csv", f"{ticker}.csv"):
            path = self.directory / candidate
            if path.exists():
                df = pd.read_csv(path)
                date_col = next(
                    (c for c in df.columns if str(c).strip().lower() in ("date", "observation_date", "time")),
                    df.columns[0],
                )
                return df.set_index(date_col)
        return None


class YahooPriceProvider(PriceProvider):
    """Yahoo Finance chart endpoint.

    Returned HTTP 429 from the build environment on every attempt. Kept because that block is
    address-specific rather than a property of the API, but it must be treated as unreliable.
    """

    name = "yahoo"
    _URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _BROWSER_UA})

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        params = {"interval": "1d", "range": "10y", "events": "div,split"}
        if start is not None and end is not None:
            params = {
                "interval": "1d",
                "period1": int(pd.Timestamp(start).timestamp()),
                "period2": int(pd.Timestamp(end).timestamp()),
            }
        resp = self._session.get(self._URL.format(ticker=ticker), params=params, timeout=self.timeout)
        if resp.status_code == 429:
            log.warning("yahoo rate-limited (429) for %s — this source is unavailable here", ticker)
            return None
        if resp.status_code != 200:
            log.warning("yahoo returned HTTP %s for %s", resp.status_code, ticker)
            return None
        payload = resp.json()
        results = (payload.get("chart") or {}).get("result") or []
        if not results:
            return None
        result = results[0]
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]
        if not stamps:
            return None
        frame = pd.DataFrame(
            {
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": quote.get("close"),
                "adj_close": adj.get("adjclose", quote.get("close")),
                "volume": quote.get("volume"),
            },
            index=pd.to_datetime(stamps, unit="s").normalize(),
        )
        return frame.dropna(how="all")


class StooqPriceProvider(PriceProvider):
    """Stooq daily CSV.

    Served a JavaScript proof-of-work bot check rather than CSV from the build environment. That is
    a deliberate anti-automation measure, so this provider detects it and gives up rather than
    attempting to solve it. Left in place because it works from browsers and many networks.
    """

    name = "stooq"
    _URL = "https://stooq.com/q/d/l/"

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        symbol = ticker.lower()
        if "." not in symbol:
            symbol = f"{symbol}.us"
        resp = requests.get(
            self._URL, params={"s": symbol, "i": "d"}, timeout=self.timeout, headers={"User-Agent": _BROWSER_UA}
        )
        if resp.status_code != 200:
            return None
        text = resp.text
        if text.lstrip().lower().startswith(("<!doctype", "<html")) or "requires JavaScript" in text:
            log.warning("stooq served a bot-check page for %s — source unavailable without a browser", ticker)
            return None
        frame = pd.read_csv(io.StringIO(text))
        if "Date" not in frame.columns:
            return None
        return frame.set_index("Date")


class TiingoPriceProvider(PriceProvider):
    """Tiingo end-of-day. Needs ``TIINGO_API_KEY``; the free tier covers most retail use."""

    name = "tiingo"
    _URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"

    def __init__(self, api_key: str | None = None, timeout: float = 20.0) -> None:
        self.api_key = api_key or os.environ.get("TIINGO_API_KEY")
        self.timeout = timeout

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        if not self.api_key:
            log.info("tiingo skipped: TIINGO_API_KEY not set")
            return None
        params = {"format": "json", "resampleFreq": "daily"}
        if start:
            params["startDate"] = str(start)
        if end:
            params["endDate"] = str(end)
        resp = requests.get(
            self._URL.format(ticker=ticker.lower()),
            params=params,
            timeout=self.timeout,
            headers={"Authorization": f"Token {self.api_key}"},
        )
        if resp.status_code != 200:
            log.warning("tiingo returned HTTP %s for %s", resp.status_code, ticker)
            return None
        rows = resp.json()
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        frame = frame.rename(columns={"adjClose": "adj_close"}).set_index("date")
        return frame


class ChainedPriceProvider(PriceProvider):
    """Try providers in order, first success wins. Records which one answered."""

    name = "chained"

    def __init__(self, providers: list[PriceProvider]) -> None:
        self.providers = providers
        self.last_source: str | None = None

    def get(self, ticker: str, *, start: date | None = None, end: date | None = None) -> pd.DataFrame | None:
        for provider in self.providers:
            frame = provider.get(ticker, start=start, end=end)
            if frame is not None and not frame.empty:
                self.last_source = provider.name
                return frame
        self.last_source = None
        log.warning("no price provider could supply %s (tried: %s)", ticker,
                    ", ".join(p.name for p in self.providers))
        return None

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        raise NotImplementedError


class RiskFreeProvider:
    """Risk-free rate from FRED — verified reachable, and needed by every risk-adjusted metric.

    A Sharpe ratio computed against an assumed 0% risk-free rate is simply a different statistic,
    and one that flatters every security in a positive-rate environment.
    """

    name = "fred"
    _URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    # 3-month T-bill is the standard short-rate proxy; 10-year for term-structure work.
    SERIES: ClassVar[dict[str, str]] = {"3m": "DTB3", "10y": "DGS10"}

    def __init__(self, cache_dir: str | Path | None = None, timeout: float = 20.0,
                 ttl_hours: float = 24.0) -> None:
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "stock-grader")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.ttl_hours = ttl_hours

    def get(self, tenor: str = "3m", *, refresh: bool = False) -> pd.Series | None:
        """Annualised risk-free rate as a decimal (0.0525 for 5.25%), indexed by date."""
        series_id = self.SERIES.get(tenor, tenor)
        cache = self.cache_dir / f"fred_{series_id}.csv"
        # A rate cached months ago is not today's rate, and this cache had no expiry at all.
        if cache.exists() and not refresh and _cache_fresh(cache, self.ttl_hours):
            try:
                frame = pd.read_csv(cache, index_col=0, parse_dates=True)
                return frame.iloc[:, 0] / 100.0
            except (OSError, ValueError, pd.errors.ParserError):
                log.debug("unreadable FRED cache %s, refetching", cache)
        try:
            resp = requests.get(self._URL, params={"id": series_id}, timeout=self.timeout)
            if resp.status_code != 200:
                log.warning("FRED returned HTTP %s for %s", resp.status_code, series_id)
                return None
            frame = pd.read_csv(io.StringIO(resp.text))
        except Exception as exc:
            log.warning("FRED fetch failed for %s: %s", series_id, exc)
            return None
        date_col = frame.columns[0]
        frame[date_col] = pd.to_datetime(frame[date_col])
        frame = frame.set_index(date_col)
        values = pd.to_numeric(frame.iloc[:, 0], errors="coerce").dropna()
        try:
            values.to_frame().to_csv(cache)
        except OSError:
            pass
        return values / 100.0


class BenchmarkProvider:
    """Market index series for the CAPM metrics, from FRED.

    ``beta``, ``capm_alpha`` and ``idiosyncratic_volatility`` all declare ``needs_benchmark`` and
    read :attr:`SecuritySnapshot.benchmark`. Nothing outside the test suite ever assigned it, so
    those three metrics could not fire in any configuration — they were permanently MISSING, and
    additionally dragged every security's coverage down for a reason that had nothing to do with
    the security.

    FRED serves the indices free and keyless (verified: SP500, NASDAQCOM, DJIA and VIXCLS all
    return HTTP 200; WILL5000IND is discontinued and 404s).

    **These are price indices, not total-return indices.** They exclude dividends, so alpha measured
    against them is overstated by roughly beta times the index dividend yield — on the order of
    1.5-2 percentage points a year for the S&P 500. Snapshots built this way are stamped
    ``benchmark_is_price_only`` so the report can say so rather than quietly flattering every alpha.
    """

    name = "fred_benchmark"

    SERIES: ClassVar[dict[str, str]] = {"SP500": "SP500", "NASDAQ": "NASDAQCOM", "DJIA": "DJIA"}

    def __init__(self, cache_dir: str | Path | None = None, timeout: float = 20.0,
                 ttl_hours: float = 24.0) -> None:
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "stock-grader")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.ttl_hours = ttl_hours

    def get(self, name: str = "SP500", *, refresh: bool = False) -> pd.DataFrame | None:
        """Index level as an OHLCV-shaped frame so the metric layer needs no special case."""
        series_id = self.SERIES.get(name.upper(), name)
        cache = self.cache_dir / f"bench_{series_id}.csv"
        frame = None
        if cache.exists() and not refresh and _cache_fresh(cache, self.ttl_hours):
            try:
                frame = pd.read_csv(cache, index_col=0, parse_dates=True)
            except (OSError, ValueError, pd.errors.ParserError):
                frame = None
        if frame is None:
            try:
                response = requests.get(
                    "https://fred.stlouisfed.org/graph/fredgraph.csv",
                    params={"id": series_id}, timeout=self.timeout,
                )
                if response.status_code != 200:
                    log.warning("FRED benchmark %s returned HTTP %s", series_id, response.status_code)
                    return None
                raw = pd.read_csv(io.StringIO(response.text))
            except Exception as exc:
                log.warning("FRED benchmark %s failed: %s", series_id, exc)
                return None
            date_col = raw.columns[0]
            raw[date_col] = pd.to_datetime(raw[date_col], errors="coerce")
            raw = raw.dropna(subset=[date_col]).set_index(date_col)
            # FRED writes "." for market holidays; coercing keeps them out as NaN rather than
            # poisoning the column's dtype.
            values = pd.to_numeric(raw.iloc[:, 0], errors="coerce").dropna()
            frame = values.to_frame("close")
            try:
                frame.to_csv(cache)
            except OSError:
                pass
        if frame is None or frame.empty:
            return None
        out = frame.rename(columns={frame.columns[0]: "close"})[["close"]].copy()
        out["adj_close"] = out["close"]
        return out
