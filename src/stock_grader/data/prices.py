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

import hashlib
import io
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from enum import Enum
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote as url_quote

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

__all__ = [
    "OHLCV_COLUMNS",
    "AdjustedPriceStatus",
    "BenchmarkProvider",
    "CSVPriceProvider",
    "ChainedPriceProvider",
    "PriceProvider",
    "PriceQualityDiagnostics",
    "RiskFreeProvider",
    "StooqPriceProvider",
    "TiingoPriceProvider",
    "YahooPriceProvider",
    "validate_price_frame",
]

OHLCV_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]
_YAHOO_MAX_DAILY_LOOKBACK_DAYS = 3653

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.^_-]{0,127}$")


class AdjustedPriceStatus(str, Enum):
    """How the canonical ``adj_close`` column was obtained."""

    NATIVE = "native"
    DERIVED_FROM_CLOSE = "derived_from_close"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class PriceQualityDiagnostics:
    """Auditable quality summary for a daily price frame.

    The object is also copied into ``DataFrame.attrs["price_quality"]`` as plain JSON-compatible
    values, so downstream code can expose the provenance without changing the provider return type.
    """

    adjusted_status: AdjustedPriceStatus
    input_rows: int
    output_rows: int
    usable_price_rows: int
    invalid_date_rows: int
    future_date_rows: int
    duplicate_date_rows: int
    nonpositive_price_rows: int
    negative_volume_rows: int
    inconsistent_ohlc_rows: int
    missing_adjusted_rows: int
    first_observation: date | None
    last_observation: date | None
    expected_business_days: int
    coverage_ratio: float | None
    longest_gap_days: int | None
    age_days: int | None
    stale: bool
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a serialization-safe representation for ``DataFrame.attrs`` and reports."""
        result = asdict(self)
        result["adjusted_status"] = self.adjusted_status.value
        result["first_observation"] = (
            self.first_observation.isoformat() if self.first_observation is not None else None
        )
        result["last_observation"] = (
            self.last_observation.isoformat() if self.last_observation is not None else None
        )
        result["warnings"] = list(self.warnings)
        return result


@dataclass(slots=True)
class _CircuitBreaker:
    """Small per-provider breaker that prevents one outage being retried for every ticker."""

    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    def allow_request(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_seconds:
            self.failures = 0
            self.opened_at = None
            return True
        return False

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None


def _cache_fresh(path: Path, ttl_hours: float) -> bool:
    """Whether a cache file is younger than its time to live."""
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0 < ttl_hours
    except OSError:
        return False


def _utc_epoch(day: date) -> int:
    """UTC midnight epoch for a calendar date, independent of the host timezone."""
    instant = datetime.combine(day, datetime_time.min, tzinfo=UTC)
    return int(instant.timestamp())


def _safe_cache_path(root: Path, namespace: str, identifier: str, suffix: str) -> Path:
    """Build a cache path that cannot escape ``root`` through input or an existing symlink."""
    root = root.resolve()
    raw = str(identifier).strip()
    if _SAFE_IDENTIFIER.fullmatch(raw) and ".." not in raw:
        component = raw
    else:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        component = f"id-{digest}"
    candidate = (root / f"{namespace}_{component}{suffix}").resolve()
    if candidate.parent != root:
        raise ValueError("cache path escaped its configured directory")
    return candidate


def _atomic_csv_write(frame: pd.DataFrame | pd.Series, destination: Path) -> None:
    """Replace a CSV cache atomically so an interrupted write cannot poison later runs."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".tmp",
            prefix=f".{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        frame.to_csv(temporary)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.debug("could not remove temporary cache file %s", temporary)


def _read_series_cache(path: Path) -> pd.Series | None:
    try:
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
    except (OSError, ValueError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return None
    if frame.empty or frame.shape[1] == 0:
        return None
    index = pd.to_datetime(frame.index, errors="coerce", utc=True)
    valid = np.asarray(index.notna(), dtype=bool)
    values = pd.to_numeric(frame.iloc[:, 0], errors="coerce").loc[valid]
    values.index = index[valid].tz_convert(None).normalize()
    values = values[~values.index.duplicated(keep="last")].dropna().sort_index()
    return values.astype("float64") if not values.empty else None


def _adjusted_status(frame: pd.DataFrame) -> AdjustedPriceStatus:
    override = frame.attrs.get("adjusted_status")
    if override in {item.value for item in AdjustedPriceStatus}:
        return AdjustedPriceStatus(str(override))
    if "adj_close" in frame.columns:
        return AdjustedPriceStatus.NATIVE
    if any(name in frame.columns for name in ("adjclose", "adj._close", "adjusted_close")):
        return AdjustedPriceStatus.NATIVE
    if "close" in frame.columns:
        return AdjustedPriceStatus.DERIVED_FROM_CLOSE
    return AdjustedPriceStatus.MISSING


def validate_price_frame(
    frame: pd.DataFrame,
    *,
    start: date | None = None,
    end: date | None = None,
    asof: date | None = None,
    stale_after_days: int = 10,
) -> tuple[pd.DataFrame, PriceQualityDiagnostics]:
    """Normalize and validate a daily price frame without changing the provider API.

    Invalid dates are dropped; duplicate dates keep the final observation; non-positive prices,
    negative volume, and internally inconsistent high/low values are quarantined as ``NaN``.
    Missing adjusted prices remain explicit in the diagnostics. A close-only feed is supported for
    compatibility, but it is marked ``derived_from_close`` because its returns exclude dividends
    and other distributions.
    """
    data = frame.copy()
    input_rows = len(data)
    data.columns = [str(column).strip().lower().replace(" ", "_") for column in data.columns]
    adjusted_status = _adjusted_status(data)

    for alias in ("adjclose", "adj._close", "adjusted_close"):
        if "adj_close" not in data.columns and alias in data.columns:
            data = data.rename(columns={alias: "adj_close"})
            break
    if "adj_close" not in data.columns and "close" in data.columns:
        data["adj_close"] = data["close"]

    parsed_index = pd.to_datetime(data.index, errors="coerce", utc=True, format="mixed")
    valid_dates = np.asarray(parsed_index.notna(), dtype=bool)
    invalid_date_rows = int((~valid_dates).sum())
    data = data.loc[valid_dates].copy()
    parsed_index = parsed_index[valid_dates].tz_convert(None).normalize()
    data.index = parsed_index

    duplicate_date_rows = int(data.index.duplicated(keep="last").sum())
    data = data[~data.index.duplicated(keep="last")].sort_index()
    for column in OHLCV_COLUMNS:
        if column not in data.columns:
            data[column] = float("nan")
    data = data[OHLCV_COLUMNS].apply(pd.to_numeric, errors="coerce").astype("float64")

    price_columns = ["open", "high", "low", "close", "adj_close"]
    nonpositive_mask = data[price_columns].le(0)
    nonpositive_price_rows = int(nonpositive_mask.any(axis=1).sum())
    data[price_columns] = data[price_columns].mask(nonpositive_mask)

    negative_volume = data["volume"].lt(0)
    negative_volume_rows = int(negative_volume.sum())
    data.loc[negative_volume, "volume"] = float("nan")

    comparable = data[["open", "high", "low", "close"]]
    inconsistent_high = data["high"].lt(comparable.max(axis=1, skipna=True))
    inconsistent_low = data["low"].gt(comparable.min(axis=1, skipna=True))
    inconsistent_ohlc = inconsistent_high | inconsistent_low
    inconsistent_ohlc_rows = int(inconsistent_ohlc.sum())
    data.loc[inconsistent_high, "high"] = float("nan")
    data.loc[inconsistent_low, "low"] = float("nan")

    reference = asof or end or date.today()
    future_dates = np.asarray(data.index > pd.Timestamp(reference), dtype=bool)
    future_date_rows = int(future_dates.sum())
    data = data.loc[~future_dates]
    if start is not None:
        data = data[data.index >= pd.Timestamp(start)]
    if end is not None:
        data = data[data.index <= pd.Timestamp(end)]
    data = data.dropna(how="all")

    output_rows = len(data)
    usable_mask = data[["close", "adj_close"]].notna().any(axis=1)
    usable_index = pd.DatetimeIndex(data.index[usable_mask])
    usable_price_rows = len(usable_index)
    first_observation = usable_index[0].date() if usable_price_rows else None
    last_observation = usable_index[-1].date() if usable_price_rows else None
    expected_business_days = (
        len(pd.bdate_range(usable_index[0], usable_index[-1]))
        if usable_price_rows > 1
        else usable_price_rows
    )
    coverage_ratio = (
        min(1.0, usable_price_rows / expected_business_days) if expected_business_days else None
    )
    gaps = (
        usable_index.to_series().diff().dt.days.dropna()
        if usable_price_rows > 1
        else pd.Series(dtype="float64")
    )
    longest_gap_days = int(gaps.max()) if not gaps.empty else None

    age_days = max(0, (reference - last_observation).days) if last_observation is not None else None
    stale = age_days is not None and age_days > stale_after_days
    missing_adjusted_rows = int(data["adj_close"].isna().sum())

    warnings: list[str] = []
    if adjusted_status is AdjustedPriceStatus.DERIVED_FROM_CLOSE:
        warnings.append(
            "adjusted close derived from close; total-return calculations exclude distributions"
        )
    elif adjusted_status is AdjustedPriceStatus.MISSING:
        warnings.append("adjusted close is unavailable")
    if missing_adjusted_rows:
        warnings.append(f"adjusted close is missing in {missing_adjusted_rows} output row(s)")
    if invalid_date_rows:
        warnings.append(f"dropped {invalid_date_rows} row(s) with invalid dates")
    if future_date_rows:
        warnings.append(f"dropped {future_date_rows} row(s) after the analysis date")
    if duplicate_date_rows:
        warnings.append(f"deduplicated {duplicate_date_rows} date row(s)")
    if nonpositive_price_rows:
        warnings.append(f"quarantined non-positive prices in {nonpositive_price_rows} row(s)")
    if negative_volume_rows:
        warnings.append(f"quarantined negative volume in {negative_volume_rows} row(s)")
    if inconsistent_ohlc_rows:
        warnings.append(f"quarantined inconsistent OHLC values in {inconsistent_ohlc_rows} row(s)")
    if usable_price_rows == 0:
        warnings.append("no usable close or adjusted-close observations")
    if coverage_ratio is not None and expected_business_days >= 20 and coverage_ratio < 0.85:
        warnings.append(f"daily coverage is sparse ({coverage_ratio:.1%} of business days)")
    if stale:
        warnings.append(f"last price is {age_days} day(s) old")

    diagnostics = PriceQualityDiagnostics(
        adjusted_status=adjusted_status,
        input_rows=input_rows,
        output_rows=output_rows,
        usable_price_rows=usable_price_rows,
        invalid_date_rows=invalid_date_rows,
        future_date_rows=future_date_rows,
        duplicate_date_rows=duplicate_date_rows,
        nonpositive_price_rows=nonpositive_price_rows,
        negative_volume_rows=negative_volume_rows,
        inconsistent_ohlc_rows=inconsistent_ohlc_rows,
        missing_adjusted_rows=missing_adjusted_rows,
        first_observation=first_observation,
        last_observation=last_observation,
        expected_business_days=expected_business_days,
        coverage_ratio=coverage_ratio,
        longest_gap_days=longest_gap_days,
        age_days=age_days,
        stale=stale,
        warnings=tuple(warnings),
    )
    data.attrs["adjusted_status"] = adjusted_status.value
    data.attrs["price_quality"] = diagnostics.as_dict()
    return data, diagnostics


def _conform(df: pd.DataFrame) -> pd.DataFrame:
    """Backward-compatible canonicalization helper used by older provider integrations."""
    conformed, _ = validate_price_frame(df)
    return conformed


class PriceProvider:
    """Base class. Subclasses override :meth:`_fetch` and never raise out of :meth:`get`."""

    name = "base"
    stale_after_days = 10
    last_diagnostics: PriceQualityDiagnostics | None = None

    def get(
        self,
        ticker: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame | None:
        try:
            df = self._fetch(ticker, start=start, end=end)
            if df is None or df.empty:
                self.last_diagnostics = None
                return None
            df, self.last_diagnostics = validate_price_frame(
                df,
                start=start,
                end=end,
                asof=end,
                stale_after_days=self.stale_after_days,
            )
        except Exception as exc:
            self.last_diagnostics = None
            log.warning("%s price fetch failed for %s: %s", self.name, ticker, exc)
            return None
        if self.last_diagnostics.stale:
            log.warning(
                "%s price data for %s is stale (%s); refusing the frame",
                self.name,
                ticker,
                self.last_diagnostics.last_observation,
            )
            return None
        return df if not df.empty and self.last_diagnostics.usable_price_rows > 0 else None

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        raise NotImplementedError


class CSVPriceProvider(PriceProvider):
    """Read prices from local CSVs — the reliable path when no API is reachable.

    Looks for ``<dir>/<TICKER>.csv`` with a date index and any subset of the OHLCV columns. A file
    with nothing but ``date,close`` is enough for every return, risk and momentum metric.
    """

    name = "csv"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        if not _SAFE_IDENTIFIER.fullmatch(ticker) or ".." in ticker:
            log.warning("refusing unsafe CSV price ticker %r", ticker)
            return None
        for candidate in (f"{ticker.upper()}.csv", f"{ticker.lower()}.csv", f"{ticker}.csv"):
            path = (self.directory / candidate).resolve()
            if path.parent != self.directory:
                log.warning("refusing CSV price path outside %s", self.directory)
                return None
            if path.is_file():
                df = pd.read_csv(path)
                date_col = next(
                    (
                        c
                        for c in df.columns
                        if str(c).strip().lower() in ("date", "observation_date", "time")
                    ),
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

    def __init__(
        self,
        timeout: float = 15.0,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _BROWSER_UA})
        self._breaker = _CircuitBreaker(
            failure_threshold=max(1, failure_threshold),
            cooldown_seconds=max(0.0, cooldown_seconds),
        )

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        if not self._breaker.allow_request():
            log.warning("yahoo circuit breaker is open; skipping %s", ticker)
            return None
        params: dict[str, str | int] = {
            "interval": "1d",
            "range": "10y",
            "events": "div,split",
        }
        if start is not None or end is not None:
            request_end = end or date.today()
            request_start = start or (request_end - timedelta(days=3653))
            if (request_end - request_start).days > _YAHOO_MAX_DAILY_LOOKBACK_DAYS:
                log.warning(
                    "yahoo daily history beyond 10 years is unreliable; refusing %s..%s for %s",
                    request_start,
                    request_end,
                    ticker,
                )
                return None
            params = {
                "interval": "1d",
                "period1": _utc_epoch(request_start),
                # Yahoo treats period2 as exclusive; include the requested end date.
                "period2": _utc_epoch(request_end + timedelta(days=1)),
                "events": "div,split",
            }
        try:
            resp = self._session.get(
                self._URL.format(ticker=url_quote(ticker, safe="")),
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException:
            self._breaker.record_failure()
            raise
        if resp.status_code in {401, 403, 429}:
            self._breaker.record_failure()
            log.warning(
                "yahoo rejected the request (HTTP %s) for %s — source temporarily unavailable",
                resp.status_code,
                ticker,
            )
            return None
        if resp.status_code >= 500:
            self._breaker.record_failure()
        elif resp.status_code != 200:
            self._breaker.record_success()
        if resp.status_code != 200:
            log.warning("yahoo returned HTTP %s for %s", resp.status_code, ticker)
            return None
        try:
            payload = resp.json()
        except (TypeError, ValueError) as exc:
            self._breaker.record_failure()
            log.warning("yahoo returned malformed JSON for %s: %s", ticker, exc)
            return None
        if not isinstance(payload, dict):
            self._breaker.record_failure()
            log.warning("yahoo returned an unexpected payload shape for %s", ticker)
            return None
        self._breaker.record_success()
        results = (payload.get("chart") or {}).get("result") or []
        if not results:
            return None
        result = results[0]
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]
        if not stamps:
            return None
        columns = {
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
        }
        adjusted = adj.get("adjclose")
        if adjusted is not None:
            columns["adj_close"] = adjusted
        frame = pd.DataFrame(columns, index=pd.to_datetime(stamps, unit="s").normalize())
        split_events: list[dict[str, object]] = []
        raw_splits = (result.get("events") or {}).get("splits") or {}
        if isinstance(raw_splits, dict):
            for raw in raw_splits.values():
                if not isinstance(raw, dict):
                    continue
                raw_date = raw.get("date")
                raw_numerator = raw.get("numerator")
                raw_denominator = raw.get("denominator")
                if raw_date is None or raw_numerator is None or raw_denominator is None:
                    continue
                try:
                    split_date = pd.to_datetime(raw_date, unit="s", utc=True, errors="raise").date()
                    numerator = float(raw_numerator)
                    denominator = float(raw_denominator)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    not np.isfinite(numerator)
                    or not np.isfinite(denominator)
                    or numerator <= 0
                    or denominator <= 0
                ):
                    continue
                split_events.append(
                    {
                        "date": split_date.isoformat(),
                        "factor": numerator / denominator,
                        "ratio": raw.get("splitRatio"),
                    }
                )
        frame.attrs["split_events"] = sorted(split_events, key=lambda item: str(item["date"]))
        return frame.dropna(how="all")


class StooqPriceProvider(PriceProvider):
    """Stooq daily CSV.

    Served a JavaScript proof-of-work bot check rather than CSV from the build environment. That is
    a deliberate anti-automation measure, so this provider detects it and gives up rather than
    attempting to solve it. Left in place because it works from browsers and many networks.
    """

    name = "stooq"
    _URL = "https://stooq.com/q/d/l/"

    def __init__(
        self,
        timeout: float = 15.0,
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _BROWSER_UA})
        self._breaker = _CircuitBreaker(
            failure_threshold=max(1, failure_threshold),
            cooldown_seconds=max(0.0, cooldown_seconds),
        )

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        if not self._breaker.allow_request():
            log.warning("stooq circuit breaker is open; skipping %s", ticker)
            return None
        symbol = ticker.lower()
        if "." not in symbol:
            symbol = f"{symbol}.us"
        try:
            resp = self._session.get(
                self._URL,
                params={"s": symbol, "i": "d"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            self._breaker.record_failure()
            raise
        if resp.status_code == 429 or resp.status_code >= 500:
            self._breaker.record_failure()
        elif resp.status_code != 200:
            self._breaker.record_success()
        if resp.status_code != 200:
            return None
        text = resp.text
        if (
            text.lstrip().lower().startswith(("<!doctype", "<html"))
            or "requires JavaScript" in text
        ):
            self._breaker.record_failure()
            log.warning(
                "stooq served a bot-check page for %s — source unavailable without a browser",
                ticker,
            )
            return None
        try:
            frame = pd.read_csv(io.StringIO(text))
        except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            self._breaker.record_failure()
            log.warning("stooq returned malformed CSV for %s: %s", ticker, exc)
            return None
        if "Date" not in frame.columns:
            self._breaker.record_failure()
            return None
        self._breaker.record_success()
        return frame.set_index("Date")


class TiingoPriceProvider(PriceProvider):
    """Tiingo end-of-day. Needs ``TIINGO_API_KEY``; the free tier covers most retail use."""

    name = "tiingo"
    _URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 20.0,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("TIINGO_API_KEY")
        self.timeout = timeout
        self._session = requests.Session()
        self._breaker = _CircuitBreaker(
            failure_threshold=max(1, failure_threshold),
            cooldown_seconds=max(0.0, cooldown_seconds),
        )

    def _fetch(self, ticker: str, *, start: date | None, end: date | None) -> pd.DataFrame | None:
        if not self.api_key:
            log.info("tiingo skipped: TIINGO_API_KEY not set")
            return None
        if not self._breaker.allow_request():
            log.warning("tiingo circuit breaker is open; skipping %s", ticker)
            return None
        params = {"format": "json", "resampleFreq": "daily"}
        if start:
            params["startDate"] = str(start)
        if end:
            params["endDate"] = str(end)
        try:
            resp = self._session.get(
                self._URL.format(ticker=url_quote(ticker.lower(), safe="")),
                params=params,
                timeout=self.timeout,
                headers={"Authorization": f"Token {self.api_key}"},
            )
        except requests.RequestException:
            self._breaker.record_failure()
            raise
        if resp.status_code in {401, 403, 429} or resp.status_code >= 500:
            self._breaker.record_failure()
        elif resp.status_code != 200:
            self._breaker.record_success()
        if resp.status_code != 200:
            log.warning("tiingo returned HTTP %s for %s", resp.status_code, ticker)
            return None
        try:
            rows = resp.json()
        except (TypeError, ValueError) as exc:
            self._breaker.record_failure()
            log.warning("tiingo returned malformed JSON for %s: %s", ticker, exc)
            return None
        if not isinstance(rows, list):
            self._breaker.record_failure()
            log.warning("tiingo returned an unexpected payload shape for %s", ticker)
            return None
        if not rows:
            self._breaker.record_success()
            return None
        frame = pd.DataFrame(rows)
        if "date" not in frame.columns or "close" not in frame.columns:
            self._breaker.record_failure()
            log.warning("tiingo payload for %s lacks date or close", ticker)
            return None
        self._breaker.record_success()
        frame = frame.rename(columns={"adjClose": "adj_close"}).set_index("date")
        return frame


class ChainedPriceProvider(PriceProvider):
    """Try providers in order, first success wins. Records which one answered."""

    name = "chained"

    def __init__(self, providers: list[PriceProvider]) -> None:
        self.providers = providers
        self.last_source: str | None = None
        self.last_diagnostics: PriceQualityDiagnostics | None = None
        self.last_rejections: list[dict[str, object]] = []

    def get(
        self,
        ticker: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame | None:
        self.last_rejections = []
        last_diagnostics: PriceQualityDiagnostics | None = None
        for provider in self.providers:
            frame = provider.get(ticker, start=start, end=end)
            if frame is not None and not frame.empty:
                self.last_source = provider.name
                self.last_diagnostics = provider.last_diagnostics
                return frame
            if provider.last_diagnostics is not None:
                last_diagnostics = provider.last_diagnostics
                self.last_rejections.append(
                    {
                        "provider": provider.name,
                        "price_quality": provider.last_diagnostics.as_dict(),
                    }
                )
        self.last_source = None
        self.last_diagnostics = last_diagnostics
        log.warning(
            "no price provider could supply %s (tried: %s)",
            ticker,
            ", ".join(provider.name for provider in self.providers),
        )
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

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        timeout: float = 20.0,
        ttl_hours: float = 24.0,
    ) -> None:
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "stock-grader").resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.ttl_hours = ttl_hours

    def get(self, tenor: str = "3m", *, refresh: bool = False) -> pd.Series | None:
        """Annualised risk-free rate as a decimal (0.0525 for 5.25%), indexed by date."""
        series_id = self.SERIES.get(tenor, tenor)
        try:
            cache = _safe_cache_path(self.cache_dir, "fred", series_id, ".csv")
        except ValueError as exc:
            log.warning("refusing unsafe FRED cache path for %s: %s", series_id, exc)
            return None
        cached = _read_series_cache(cache) if cache.is_file() else None
        if cached is not None and not refresh and _cache_fresh(cache, self.ttl_hours):
            result = cached / 100.0
            result.attrs["cache_status"] = "fresh"
            return result

        def stale_fallback() -> pd.Series | None:
            if cached is None:
                return None
            log.warning("using stale FRED cache for %s after refresh failure", series_id)
            result = cached / 100.0
            result.attrs["cache_status"] = "stale_fallback"
            return result

        try:
            resp = requests.get(self._URL, params={"id": series_id}, timeout=self.timeout)
            if resp.status_code != 200:
                log.warning("FRED returned HTTP %s for %s", resp.status_code, series_id)
                return stale_fallback()
            frame = pd.read_csv(io.StringIO(resp.text))
        except Exception as exc:
            log.warning("FRED fetch failed for %s: %s", series_id, exc)
            return stale_fallback()
        if frame.empty or frame.shape[1] < 2:
            log.warning("FRED returned an empty or malformed series for %s", series_id)
            return stale_fallback()
        date_col = frame.columns[0]
        frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce", utc=True)
        frame = frame.dropna(subset=[date_col]).set_index(date_col)
        frame.index = pd.DatetimeIndex(frame.index).tz_convert(None).normalize()
        values = pd.to_numeric(frame.iloc[:, 0], errors="coerce").dropna()
        values = values[~values.index.duplicated(keep="last")].sort_index()
        if values.empty:
            log.warning("FRED returned no numeric observations for %s", series_id)
            return stale_fallback()
        try:
            _atomic_csv_write(values.to_frame("value"), cache)
        except (OSError, ValueError):
            log.debug("could not write FRED cache %s", cache)
        result = values / 100.0
        result.attrs["cache_status"] = "refreshed"
        return result


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

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        timeout: float = 20.0,
        ttl_hours: float = 24.0,
    ) -> None:
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "stock-grader").resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.ttl_hours = ttl_hours
        self.last_diagnostics: PriceQualityDiagnostics | None = None

    def get(self, name: str = "SP500", *, refresh: bool = False) -> pd.DataFrame | None:
        """Index level as an OHLCV-shaped frame so the metric layer needs no special case."""
        series_id = self.SERIES.get(name.upper(), name)
        try:
            cache = _safe_cache_path(self.cache_dir, "bench", series_id, ".csv")
        except ValueError as exc:
            self.last_diagnostics = None
            log.warning("refusing unsafe benchmark cache path for %s: %s", series_id, exc)
            return None
        cached = _read_series_cache(cache) if cache.is_file() else None
        values: pd.Series | None = None
        cache_status = "miss"
        if cached is not None and not refresh and _cache_fresh(cache, self.ttl_hours):
            values = cached
            cache_status = "fresh"
        if values is None:
            try:
                response = requests.get(
                    "https://fred.stlouisfed.org/graph/fredgraph.csv",
                    params={"id": series_id},
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    raise requests.HTTPError(
                        f"FRED benchmark {series_id} returned HTTP {response.status_code}"
                    )
                raw = pd.read_csv(io.StringIO(response.text))
            except Exception as exc:
                log.warning("FRED benchmark %s failed: %s", series_id, exc)
                if cached is None:
                    self.last_diagnostics = None
                    return None
                log.warning("using stale FRED benchmark cache for %s", series_id)
                values = cached
                cache_status = "stale_fallback"
            if values is None:
                if raw.empty or raw.shape[1] < 2:
                    log.warning(
                        "FRED benchmark %s returned an empty or malformed series", series_id
                    )
                    if cached is None:
                        self.last_diagnostics = None
                        return None
                    values = cached
                    cache_status = "stale_fallback"
                else:
                    date_col = raw.columns[0]
                    raw[date_col] = pd.to_datetime(raw[date_col], errors="coerce", utc=True)
                    raw = raw.dropna(subset=[date_col]).set_index(date_col)
                    raw.index = pd.DatetimeIndex(raw.index).tz_convert(None).normalize()
                    # FRED writes "." for market holidays; coercing keeps them out as NaN rather
                    # than poisoning the column's dtype.
                    values = pd.to_numeric(raw.iloc[:, 0], errors="coerce").dropna()
                    values = values[~values.index.duplicated(keep="last")].sort_index()
                    if values.empty and cached is not None:
                        log.warning(
                            "FRED benchmark %s returned no numeric observations; using stale cache",
                            series_id,
                        )
                        values = cached
                        cache_status = "stale_fallback"
                    else:
                        cache_status = "refreshed"
            if cache_status == "refreshed" and values is not None and not values.empty:
                try:
                    _atomic_csv_write(values.to_frame("close"), cache)
                except (OSError, ValueError):
                    log.debug("could not write FRED benchmark cache %s", cache)

        if values is None or values.empty:
            self.last_diagnostics = None
            return None
        out, self.last_diagnostics = validate_price_frame(values.to_frame("close"))
        out.attrs["cache_status"] = cache_status
        return out
