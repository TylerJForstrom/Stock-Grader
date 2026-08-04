"""Share prices derived from SEC filings — the workaround for having no market-data feed.

No free price API was reachable when this was built (Yahoo 429s, Stooq serves a bot-check), which
disabled every valuation metric. But the SEC publishes two things that together pin down a price,
free and keyless:

**1. Insider transaction prices (the good source).** Every Form 4 reports the per-share price of an
insider's trade. Open-market sales (``S``), open-market purchases (``P``) and shares withheld for
taxes (``F``) all transact at the prevailing market price. SEC publishes these quarterly as a ~8 MB
TSV bundle covering **~3,000 tickers per quarter**.

Measured against known market ranges for 2025Q3, the median insider price landed inside the real
range for every one of eight test companies — AAPL 227.63, WMT 98.02, JPM 297.94, NVDA 174.88.

Excluded on purpose: ``M`` (option exercise, priced at the strike, not the market), ``A`` (grant),
``G`` (gift), ``D`` (disposition to the issuer). Including ``M`` would drag the estimate toward
years-old strike prices.

**2. Public float (the fallback).** ``dei:EntityPublicFloat`` is a dollar market value with an
explicit measurement date on every 10-K cover page. Dividing by shares outstanding implies a price —
but the float **excludes affiliate holdings**, so it systematically *understates*. Measured error
was under 3% for six of eight companies and catastrophic for the other two: Walmart -50% (the
Walton family holds about half the shares) and Simon Property -37%.

That failure is one-directional — it always makes a stock look cheaper than it is, which is the
worst possible bias for a value screen. So the float path is only used with a **calibrated
non-affiliate fraction**, solved from an insider price where one exists:

    non_affiliate_fraction = public_float / (insider_price * shares_outstanding)

Solving that recovered 49.9% for Walmart and 63.9% for Simon Property, against roughly 95% for the
widely-held names — i.e. it correctly identifies exactly the companies where the naive float price
would have been wrong.

**What this does and does not unlock.** These prices are *sparse* — a handful of dates per quarter,
not a daily series. That is enough for every valuation metric, which needs one price and a share
count. It is **not** enough for volatility, beta, drawdown or momentum, which need daily continuity.
Those stay disabled, and :attr:`SecuritySnapshot.prices` is deliberately left unset so the metric
engine's history checks keep them that way rather than computing a statistic from twelve points.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
import pandas as pd

from .cache import default_cache_dir
from .symbols import ticker_variants

log = logging.getLogger(__name__)

__all__ = [
    "MARKET_PRICED_CODES",
    "SECInsiderPriceProvider",
    "calibrate_non_affiliate_fraction",
    "calibrated_price_from_float",
    "check_price_share_basis",
    "implied_price_from_float",
    "resolve_price",
]

# Form 4 transaction codes that execute at the prevailing market price.
#   S = open-market sale, P = open-market purchase, F = shares withheld at market to cover tax.
# Deliberately excludes M (option exercise at strike), A (award), G (gift), D (disposition to issuer).
MARKET_PRICED_CODES = frozenset({"S", "P", "F"})

_DATASET_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{quarter}_form345.zip"
_DEFAULT_CONTACT = "stock-grader (set STOCK_GRADER_CONTACT to your email)"
_QUARTER_PATTERN = re.compile(r"^(?P<year>20\d{2})q(?P<quarter>[1-4])$")
_PRICE_COLUMNS = ["ticker", "date", "price"]


def _quarter_bounds(quarter: str) -> tuple[date, date]:
    """Plausible transaction-date window for a ``YYYYqN`` bundle, with a quarter of slack."""
    match = _QUARTER_PATTERN.fullmatch(quarter)
    if match is None:
        raise ValueError(f"invalid SEC quarter identifier: {quarter!r}")
    year, q = int(match.group("year")), int(match.group("quarter"))
    start_month = 3 * (q - 1) + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    end = date(year, end_month, 28) + timedelta(days=4)
    end = end - timedelta(days=end.day)  # last day of the quarter's final month
    return (start - timedelta(days=95), end + timedelta(days=95))


# Bump when the derived-table logic changes (filtering, clipping, median rule):
# the parquet caches a DERIVED table, so old files stay schema-valid but
# semantically stale forever unless the version participates in the filename.
# v3: tickers are canonicalized to the SEC dash form on read, so one issuer's
# dot/space/dash spellings collapse into a single median row per day.
_CACHE_SCHEMA_VERSION = 3


def _quarter_cache_path(root: Path, quarter: str) -> Path:
    """Resolve a known-format quarter path and refuse symlink escapes."""
    if _QUARTER_PATTERN.fullmatch(quarter) is None:
        raise ValueError(f"invalid SEC quarter identifier: {quarter!r}")
    root = root.resolve()
    candidate = (root / f"{quarter}_v{_CACHE_SCHEMA_VERSION}.parquet").resolve()
    if candidate.parent != root:
        raise ValueError("SEC insider cache path escaped its configured directory")
    return candidate


def _normalize_price_table(frame: pd.DataFrame, quarter: str) -> pd.DataFrame | None:
    """Validate cached or downloaded rows before they enter the in-memory table."""
    if not set(_PRICE_COLUMNS).issubset(frame.columns):
        return None
    table = frame[_PRICE_COLUMNS].copy()
    # Canonicalize on read (vectorized ``canonical_ticker``): filers write the
    # same class share as BRK-B, BRK.B, or BRK B, and each spelling would
    # otherwise keep its own row and split the per-day median.
    table["ticker"] = (
        table["ticker"]
        .astype(str)
        .str.upper()
        .str.strip()
        .str.replace(".", "-", regex=False)
        .str.replace(" ", "-", regex=False)
        .str.replace(r"-{2,}", "-", regex=True)
    )
    table["date"] = pd.to_datetime(table["date"], errors="coerce", utc=True)
    table["date"] = table["date"].dt.tz_convert(None).dt.normalize()
    table["price"] = pd.to_numeric(table["price"], errors="coerce")
    table = table.dropna(subset=_PRICE_COLUMNS)
    table = table[table["ticker"].ne("") & table["ticker"].ne("NONE") & table["price"].gt(0)]
    low, high = _quarter_bounds(quarter)
    table = table[table["date"].between(pd.Timestamp(low), pd.Timestamp(high), inclusive="both")]
    if table.empty:
        return pd.DataFrame(columns=_PRICE_COLUMNS)
    grouped = table.groupby(["ticker", "date"], as_index=False).agg(price=("price", "median"))
    return grouped.sort_values(["ticker", "date"]).reset_index(drop=True)


def _atomic_parquet_write(frame: pd.DataFrame, destination: Path) -> None:
    temporary: Path | None = None
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
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.debug("could not remove temporary insider cache %s", temporary)


class _InsiderClient(Protocol):
    """The one method this provider needs from an SEC client.

    Typed as a Protocol rather than `object` so the hardened-surface type check
    can actually see the call at :meth:`_fetch_quarter`; `object | None` made
    `self._client.get_bytes(url)` unverifiable, which is the opposite of what a
    blocking gate over this file is for.
    """

    def get_bytes(self, url: str) -> bytes | None: ...


class SECInsiderPriceProvider:
    """Sparse share prices from SEC Form 345 insider-transaction data sets.

    Downloads one ~8 MB zip per calendar quarter and caches the extracted price table. Coverage is
    roughly 3,000 tickers per quarter — every company whose insiders traded.
    """

    name = "sec_insider"
    provides_prices = True
    is_sparse = True

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        contact: str | None = None,
        quarters: int = 4,
        timeout: float = 180.0,
        failure_threshold: int = 2,
        cooldown_seconds: float = 60.0,
        client: _InsiderClient | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir or default_cache_dir("insider")).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.contact = contact or os.environ.get("STOCK_GRADER_CONTACT") or _DEFAULT_CONTACT
        # Every request to sec.gov hosts goes through a SECClient so the
        # fair-access limiter, declared User-Agent, and retry policy are shared
        # rather than re-implemented per module.
        from .sec import SECClient

        self._client = client or SECClient(cache_dir=self.cache_dir, contact=self.contact)
        self.quarters = max(1, quarters)
        self.timeout = timeout
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._network_failures = 0
        self._circuit_opened_at: float | None = None
        self._tables: dict[tuple[str, ...], pd.DataFrame] = {}
        # Kept as the most recently requested table for compatibility with integrations that
        # inspect it, but it is no longer the cache key.
        self._table: pd.DataFrame | None = None

    def _network_allowed(self) -> bool:
        if self._circuit_opened_at is None:
            return True
        if time.monotonic() - self._circuit_opened_at >= self.cooldown_seconds:
            self._network_failures = 0
            self._circuit_opened_at = None
            return True
        return False

    def _record_network_failure(self) -> None:
        self._network_failures += 1
        if self._network_failures >= self.failure_threshold:
            self._circuit_opened_at = time.monotonic()

    def _record_network_success(self) -> None:
        self._network_failures = 0
        self._circuit_opened_at = None

    # ---------------------------------------------------------------- fetching

    @staticmethod
    def _recent_quarters(asof: date, n: int) -> list[str]:
        """The n most recent completed calendar quarters, newest first, as ``YYYYqN``.

        SEC publishes a quarter's bundle some weeks after it closes, so the quarter containing
        ``asof`` is skipped rather than requested and 404'd.
        """
        year, quarter = asof.year, (asof.month - 1) // 3 + 1
        out = []
        for _ in range(n):
            quarter -= 1
            if quarter == 0:
                quarter, year = 4, year - 1
            out.append(f"{year}q{quarter}")
        return out

    def _load_quarter(self, quarter: str, *, refresh: bool = False) -> pd.DataFrame | None:
        """Fetch one quarter and reduce it to (ticker, date, price) rows."""
        try:
            cache = _quarter_cache_path(self.cache_dir, quarter)
        except ValueError as exc:
            log.warning("%s", exc)
            return None
        if cache.is_file() and not refresh:
            try:
                cached = _normalize_price_table(pd.read_parquet(cache), quarter)
                if cached is not None:
                    return cached
                log.warning("insider cache %s has an invalid schema; refetching", cache)
            except Exception:
                log.debug("unreadable insider cache %s, refetching", cache)

        if not self._network_allowed():
            log.warning("SEC insider circuit breaker is open; skipping %s", quarter)
            return None
        url = _DATASET_URL.format(quarter=quarter)
        content = self._client.get_bytes(url)
        if content is None:
            self._record_network_failure()
            log.warning("insider dataset %s unavailable via SEC client", quarter)
            return None
        self._record_network_success()

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                submissions = pd.read_csv(
                    archive.open("SUBMISSION.tsv"),
                    sep="\t",
                    low_memory=False,
                    usecols=["ACCESSION_NUMBER", "ISSUERTRADINGSYMBOL"],
                )
                transactions = pd.read_csv(
                    archive.open("NONDERIV_TRANS.tsv"),
                    sep="\t",
                    low_memory=False,
                    usecols=[
                        "ACCESSION_NUMBER",
                        "TRANS_DATE",
                        "TRANS_CODE",
                        "TRANS_PRICEPERSHARE",
                        "TRANS_SHARES",
                    ],
                )
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            self._record_network_failure()
            log.warning("insider dataset %s could not be parsed: %s", quarter, exc)
            return None

        merged = transactions.merge(submissions, on="ACCESSION_NUMBER", how="inner")
        merged = merged[merged["TRANS_CODE"].isin(MARKET_PRICED_CODES)]
        merged["price"] = pd.to_numeric(merged["TRANS_PRICEPERSHARE"], errors="coerce")
        merged = merged[merged["price"] > 0]
        merged["ticker"] = merged["ISSUERTRADINGSYMBOL"].astype(str).str.upper().str.strip()
        merged["date"] = pd.to_datetime(merged["TRANS_DATE"], errors="coerce", format="mixed")
        merged = merged.dropna(subset=["date", "ticker"])
        merged = merged[merged["ticker"].ne("") & merged["ticker"].ne("NONE")]

        # Filers mistype transaction dates. The raw 2026q2 bundle contained dates ranging from 2002
        # to 2027 — a decade before the quarter and a year into the future. A future-dated price
        # silently becomes "the latest price" and wins every staleness check, so the window is
        # enforced here rather than trusted: one quarter of slack either side of the bundle's own
        # period covers legitimate late filings and amendments.
        low, high = _quarter_bounds(quarter)
        before = len(merged)
        merged = merged[
            (merged["date"] >= pd.Timestamp(low)) & (merged["date"] <= pd.Timestamp(high))
        ]
        dropped = before - len(merged)
        if dropped:
            log.info(
                "insider dataset %s: dropped %d transactions dated outside %s..%s",
                quarter,
                dropped,
                low,
                high,
            )

        # One price per ticker per day: the median across that day's transactions, which is robust
        # to a single mispunched filing.
        table = _normalize_price_table(
            merged[["ticker", "date", "price"]],
            quarter,
        )
        if table is None:
            self._record_network_failure()
            log.warning("insider dataset %s did not contain the required price columns", quarter)
            return None
        self._record_network_success()
        try:
            _atomic_parquet_write(table, cache)
        except Exception as exc:
            log.debug("could not cache insider prices for %s: %s", quarter, exc)
        return table

    def load(self, *, asof: date | None = None, refresh: bool = False) -> pd.DataFrame:
        """Load and concatenate the recent quarters into one price table."""
        asof = asof or date.today()
        quarter_key = tuple(self._recent_quarters(asof, self.quarters))
        if quarter_key in self._tables and not refresh:
            self._table = self._tables[quarter_key]
            return self._table
        previous = self._tables.get(quarter_key)
        frames = []
        complete = True
        for quarter in quarter_key:
            frame = self._load_quarter(quarter, refresh=refresh)
            if frame is None:
                complete = False
            elif not frame.empty:
                frames.append(frame)
        if not complete and previous is not None:
            log.warning("SEC insider refresh was incomplete; retaining prior in-memory table")
            self._table = previous
            return self._table
        if frames:
            combined = pd.concat(frames, ignore_index=True)
            self._table = combined.groupby(["ticker", "date"], as_index=False).agg(
                price=("price", "median")
            )
            self._table = self._table.sort_values(["ticker", "date"]).reset_index(drop=True)
            if complete:
                self._tables[quarter_key] = self._table
        else:
            # Do not memoize a transient all-network failure. The next call may occur after the
            # breaker cools down or after connectivity returns.
            self._table = pd.DataFrame(columns=_PRICE_COLUMNS)
            if complete:
                self._tables[quarter_key] = self._table
        return self._table

    # ---------------------------------------------------------------- querying

    def price_series(self, ticker: str, *, asof: date | None = None) -> pd.Series | None:
        """Every observed insider-transaction price for a ticker, indexed by date."""
        table = self.load(asof=asof)
        if table.empty:
            return None
        # The table stores canonical dash-form tickers; callers may arrive with
        # the dot or space spelling, so every variant is tried.
        rows = table[table["ticker"].isin(ticker_variants(ticker))]
        if rows.empty:
            return None
        series = rows.set_index("date")["price"].sort_index()
        if asof is not None:
            series = series[series.index <= pd.Timestamp(asof)]
        return series if not series.empty else None

    def latest_price(
        self, ticker: str, *, asof: date | None = None, max_age_days: int = 120
    ) -> tuple[float, date] | None:
        """Most recent insider-transaction price, with its date, if recent enough to be usable.

        A price is a point-in-time observation; a six-month-old one is not today's price. The age
        limit makes staleness an explicit refusal rather than a silent inaccuracy, and the returned
        date lets the caller report exactly how stale the figure it used was.
        """
        series = self.price_series(ticker, asof=asof)
        if series is None or series.empty:
            return None
        # Median of the most recent trading day's transactions, then guard staleness.
        observed = series.index[-1].date()
        reference = asof or date.today()
        if (reference - observed).days > max_age_days:
            log.info(
                "%s: newest insider price is %s, older than %d days", ticker, observed, max_age_days
            )
            return None
        # Average the last few observations to damp a single unusual trade.
        window = series[series.index >= series.index[-1] - pd.Timedelta(days=10)]
        return (float(window.median()), observed)

    def any_price(self, ticker: str, *, asof: date | None = None) -> tuple[float, date] | None:
        """Newest insider price at any age, for calibration rather than for pricing.

        The non-affiliate share of a company is a structural fact that moves slowly — a founder's
        stake does not change week to week — so a price far too stale to *be* the price is still
        perfectly good for solving what fraction of the float is public. Keeping this separate from
        :meth:`latest_price` means staleness is refused where it matters and exploited where it
        does not.
        """
        series = self.price_series(ticker, asof=asof)
        if series is None or series.empty:
            return None
        return (float(series.iloc[-1]), series.index[-1].date())

    def coverage(self, *, asof: date | None = None) -> int:
        """How many distinct tickers currently carry a price."""
        table = self.load(asof=asof)
        if asof is not None and not table.empty:
            table = table[table["date"] <= pd.Timestamp(asof)]
        return 0 if table.empty else int(table["ticker"].nunique())


def resolve_price(
    ticker: str,
    *,
    asof: date,
    insider: SECInsiderPriceProvider | None,
    public_float: float | None,
    float_history: pd.Series | None,
    shares_outstanding: float | None,
    max_age_days: int = 400,
) -> dict | None:
    """Pick the best available price for a ticker, preferring the **freshest** source.

    Candidate sources are not ranked by kind but by date, because staleness dominates source
    quality here: rejecting a 131-day-old insider transaction only to fall back on a 480-day-old
    public float — which is what a fixed priority order did to Apple — trades a good number for a
    much worse one. Each candidate carries its own observation date, the newest wins, and the age
    is returned so the caller can report exactly how old the figure behind a valuation is.

    Returns ``{price, source, date, age_days, non_affiliate_fraction}`` or ``None``.
    """
    candidates: list[dict] = []

    if insider is not None:
        observed = insider.any_price(ticker, asof=asof)
        if observed is not None:
            price, when = observed
            candidates.append(
                {
                    "price": price,
                    "source": "sec_insider",
                    "date": when,
                    "non_affiliate_fraction": None,
                    "valuation_eligible": True,
                }
            )

        calibrated = calibrated_price_from_float(
            float_history, shares_outstanding, insider.price_series(ticker, asof=asof)
        )
        if calibrated is not None:
            price, fraction, when = calibrated
            candidates.append(
                {
                    "price": price,
                    "source": "public_float_calibrated",
                    "date": when,
                    "non_affiliate_fraction": fraction,
                    "valuation_eligible": True,
                }
            )

    if not candidates and public_float and float_history is not None and not float_history.empty:
        implied = implied_price_from_float(public_float, shares_outstanding)
        if implied is not None:
            candidates.append(
                {
                    "price": implied,
                    "source": "public_float_lower_bound",
                    "date": pd.Timestamp(float_history.index[-1]).date(),
                    "non_affiliate_fraction": None,
                    "valuation_eligible": False,
                }
            )

    if not candidates:
        return None

    best = max(candidates, key=lambda c: c["date"])
    best["age_days"] = (asof - best["date"]).days
    if best["age_days"] > max_age_days:
        return None
    return best


def check_price_share_basis(
    price_history: pd.Series | None,
    float_history: pd.Series | None,
    shares_history: pd.Series | None,
    *,
    max_price_gap_days: int = 10,
    max_share_gap_days: int = 200,
    max_public_to_total_ratio: float = 1.25,
) -> dict[str, Any] | None:
    """Check whether split-adjusted prices and historical DEI shares use compatible units.

    Yahoo rewrites old closes onto today's split basis, while point-in-time DEI cover-page share
    counts remain on the historical basis.  On the public-float measurement date,
    ``public_float / price`` is an implied *public* share count and therefore cannot materially
    exceed total DEI shares.  A ratio above the conservative bound is a unit contradiction, so
    callers should retain the series for return calculations but quarantine the scalar price from
    market-cap and multiple calculations.

    ``None`` means that no float observation had both a nearby prior trading bar and a nearby DEI
    share observation, so the available data could not support the check.
    """
    if price_history is None or float_history is None or shares_history is None:
        return None
    if price_history.empty or float_history.empty or shares_history.empty:
        return None

    def clean(series: pd.Series) -> pd.Series:
        values = pd.to_numeric(series, errors="coerce")
        index = pd.to_datetime(values.index, errors="coerce", utc=True)
        valid = np.asarray(index.notna(), dtype=bool)
        values = values.loc[valid]
        values.index = index[valid].tz_convert(None).normalize()
        return values[~values.index.duplicated(keep="last")].dropna().sort_index()

    prices = clean(price_history)
    floats = clean(float_history)
    shares = clean(shares_history)
    if prices.empty or floats.empty or shares.empty:
        return None

    for float_timestamp in reversed(floats.index):
        price_candidates = prices.loc[:float_timestamp]
        if price_candidates.empty:
            continue
        price_timestamp = price_candidates.index[-1]
        if int((float_timestamp - price_timestamp).days) > max_price_gap_days:
            continue

        share_offsets = np.abs((shares.index - float_timestamp).days)
        share_position = int(np.argmin(share_offsets))
        share_timestamp = shares.index[share_position]
        if int(abs((share_timestamp - float_timestamp).days)) > max_share_gap_days:
            continue

        price = float(price_candidates.iloc[-1])
        public_float = float(floats.loc[float_timestamp])
        total_shares = float(shares.iloc[share_position])
        if not all(
            np.isfinite(value) and value > 0 for value in (price, public_float, total_shares)
        ):
            continue
        implied_public_shares = public_float / price
        ratio = implied_public_shares / total_shares
        return {
            # A low ratio does not prove compatibility: a large affiliate stake can mask a split
            # factor exactly.  This check can prove a contradiction, never prove equivalence.
            "status": "mismatch" if ratio > max_public_to_total_ratio else "not_contradicted",
            "public_to_total_share_ratio": float(ratio),
            "implied_public_shares": float(implied_public_shares),
            "dei_total_shares": total_shares,
            "price_date": price_timestamp.date().isoformat(),
            "public_float_date": float_timestamp.date().isoformat(),
            "shares_date": share_timestamp.date().isoformat(),
            "max_public_to_total_ratio": float(max_public_to_total_ratio),
        }
    return None


def implied_price_from_float(
    public_float: float | None,
    shares_outstanding: float | None,
    *,
    non_affiliate_fraction: float | None = None,
) -> float | None:
    """Price implied by ``dei:EntityPublicFloat``, corrected for affiliate holdings.

    ``price = public_float / (shares_outstanding * non_affiliate_fraction)``

    Without the correction this understates price by however much of the company insiders hold —
    measured at 50% for Walmart and 37% for Simon Property, always in the direction that makes a
    stock look cheap. So an uncalibrated call assumes 100% non-affiliate ownership and is a
    documented **lower bound**, not an estimate; callers that cannot supply a fraction should treat
    the result accordingly.

    Args:
        non_affiliate_fraction: solve it as ``public_float / (known_price * shares)`` from an
            insider-transaction price. Values below ~0.75 mark a genuinely insider-heavy company.
    """
    if not public_float or not shares_outstanding or shares_outstanding <= 0 or public_float <= 0:
        return None
    fraction = 1.0 if non_affiliate_fraction is None else float(non_affiliate_fraction)
    if not 0.05 <= fraction <= 1.0:
        return None
    price = public_float / (shares_outstanding * fraction)
    return float(price) if price > 0 else None


def calibrate_non_affiliate_fraction(
    public_float: float | None,
    shares_outstanding: float | None,
    known_price: float | None,
    *,
    float_date: date | None = None,
    price_date: date | None = None,
    max_gap_days: int = 45,
) -> float | None:
    """Solve for the non-affiliate share of a company from a *contemporaneous* market price.

    ``fraction = public_float / (price * shares_outstanding)``

    **The dates must line up.** The float is a dollar value measured on one specific day; the price
    is from another. Divide a float measured in March 2025 by a price from March 2026 and the
    resulting "fraction" silently absorbs a year of price movement — Apple came out at 88.6%
    non-affiliate that way, a number that describes the stock's return, not its ownership. Worse,
    feeding that fraction back through :func:`implied_price_from_float` reproduces the input price
    exactly, so the error is invisible: the pipeline looks like it calibrated something when it
    merely round-tripped.

    Pairs more than ``max_gap_days`` apart are therefore refused rather than fudged.

    Recovered 49.9% for Walmart and 63.9% for Simon Property against ~95% for widely-held names,
    which is exactly the set where the uncorrected float price is badly wrong.
    """
    if not public_float or not shares_outstanding or not known_price:
        return None
    if shares_outstanding <= 0 or known_price <= 0:
        return None
    if float_date is not None and price_date is not None:
        if abs((float_date - price_date).days) > max_gap_days:
            log.debug(
                "refusing calibration: float dated %s vs price %s (%d days apart)",
                float_date,
                price_date,
                abs((float_date - price_date).days),
            )
            return None
    fraction = public_float / (known_price * shares_outstanding)
    # Slightly over 1 is a timing/rounding artefact (float and share count measured days apart),
    # so clamp; far outside means the inputs genuinely disagree and no fraction is returned.
    if 1.0 < fraction <= 1.15:
        return 1.0
    return float(fraction) if 0.05 <= fraction <= 1.0 else None


def calibrated_price_from_float(
    float_history: pd.Series | None,
    shares_outstanding: float | None,
    price_history: pd.Series | None,
    *,
    max_gap_days: int = 45,
) -> tuple[float, float, date] | None:
    """Current price from the latest public float, corrected by a historically-calibrated fraction.

    Returns ``(price, fraction, float_date)``.

    This is the non-circular construction: the affiliate fraction is solved from an **older**
    float/price pair that share a date, and then applied to the **newest** float. Because ownership
    structure moves far more slowly than price, an old fraction is still informative about today,
    while an old price is not.

    Args:
        float_history: ``dei:EntityPublicFloat`` values indexed by measurement date.
        price_history: observed market prices indexed by date.
    """
    if float_history is None or price_history is None or not shares_outstanding:
        return None
    if float_history.empty or price_history.empty or shares_outstanding <= 0:
        return None

    # Find the float observation with the closest price observation, and calibrate on that pair.
    best: tuple[int, float, date] | None = None
    for f_date, f_value in float_history.items():
        f_day = pd.Timestamp(cast(Any, f_date)).date()
        nearest = min(price_history.index, key=lambda d: abs((pd.Timestamp(d).date() - f_day).days))
        gap = abs((pd.Timestamp(nearest).date() - f_day).days)
        if gap > max_gap_days:
            continue
        fraction = calibrate_non_affiliate_fraction(
            float(f_value),
            shares_outstanding,
            float(price_history.loc[nearest]),
            float_date=f_day,
            price_date=pd.Timestamp(nearest).date(),
            max_gap_days=max_gap_days,
        )
        if fraction is None:
            continue
        if best is None or gap < best[0]:
            best = (gap, fraction, f_day)
    if best is None:
        return None

    _, fraction, _ = best
    latest_date = pd.Timestamp(float_history.index[-1]).date()
    price = implied_price_from_float(
        float(float_history.iloc[-1]), shares_outstanding, non_affiliate_fraction=fraction
    )
    return None if price is None else (price, fraction, latest_date)
