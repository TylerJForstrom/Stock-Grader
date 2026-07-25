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
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

__all__ = ["SECInsiderPriceProvider", "implied_price_from_float", "MARKET_PRICED_CODES"]

# Form 4 transaction codes that execute at the prevailing market price.
#   S = open-market sale, P = open-market purchase, F = shares withheld at market to cover tax.
# Deliberately excludes M (option exercise at strike), A (award), G (gift), D (disposition to issuer).
MARKET_PRICED_CODES = frozenset({"S", "P", "F"})

_DATASET_URL = (
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{quarter}_form345.zip"
)
_DEFAULT_CONTACT = "stock-grader (set STOCK_GRADER_CONTACT to your email)"


def _quarter_bounds(quarter: str) -> tuple[date, date]:
    """Plausible transaction-date window for a ``YYYYqN`` bundle, with a quarter of slack."""
    year, q = int(quarter[:4]), int(quarter[-1])
    start_month = 3 * (q - 1) + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    end = date(year, end_month, 28) + timedelta(days=4)
    end = end - timedelta(days=end.day)  # last day of the quarter's final month
    return (start - timedelta(days=95), end + timedelta(days=95))


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
    ) -> None:
        import os

        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "stock-grader" / "insider")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.contact = contact or os.environ.get("STOCK_GRADER_CONTACT") or _DEFAULT_CONTACT
        self.quarters = quarters
        self.timeout = timeout
        self._table: pd.DataFrame | None = None

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
        cache = self.cache_dir / f"{quarter}.parquet"
        if cache.exists() and not refresh:
            try:
                return pd.read_parquet(cache)
            except Exception:  # noqa: BLE001 - a corrupt cache should refetch, not crash
                log.debug("unreadable insider cache %s, refetching", cache)

        url = _DATASET_URL.format(quarter=quarter)
        try:
            response = requests.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": f"Stock-Grader/0.1 ({self.contact})"},
            )
        except requests.RequestException as exc:
            log.warning("insider dataset %s unreachable: %s", quarter, exc)
            return None
        if response.status_code != 200:
            log.info("insider dataset %s returned HTTP %s", quarter, response.status_code)
            return None

        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                submissions = pd.read_csv(
                    archive.open("SUBMISSION.tsv"), sep="\t", low_memory=False,
                    usecols=["ACCESSION_NUMBER", "ISSUERTRADINGSYMBOL"],
                )
                transactions = pd.read_csv(
                    archive.open("NONDERIV_TRANS.tsv"), sep="\t", low_memory=False,
                    usecols=["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE",
                             "TRANS_PRICEPERSHARE", "TRANS_SHARES"],
                )
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
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
        merged = merged[(merged["date"] >= pd.Timestamp(low)) & (merged["date"] <= pd.Timestamp(high))]
        dropped = before - len(merged)
        if dropped:
            log.info("insider dataset %s: dropped %d transactions dated outside %s..%s",
                     quarter, dropped, low, high)

        # One price per ticker per day: the median across that day's transactions, which is robust
        # to a single mispunched filing.
        table = (
            merged.groupby(["ticker", "date"])["price"]
            .median()
            .reset_index()
            .sort_values(["ticker", "date"])
        )
        try:
            table.to_parquet(cache, index=False)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not cache insider prices for %s: %s", quarter, exc)
        return table

    def load(self, *, asof: date | None = None, refresh: bool = False) -> pd.DataFrame:
        """Load and concatenate the recent quarters into one price table."""
        if self._table is not None and not refresh:
            return self._table
        asof = asof or date.today()
        frames = []
        for quarter in self._recent_quarters(asof, self.quarters):
            frame = self._load_quarter(quarter, refresh=refresh)
            if frame is not None and not frame.empty:
                frames.append(frame)
        self._table = (
            pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"])
            if frames
            else pd.DataFrame(columns=["ticker", "date", "price"])
        )
        return self._table

    # ---------------------------------------------------------------- querying

    def price_series(self, ticker: str, *, asof: date | None = None) -> pd.Series | None:
        """Every observed insider-transaction price for a ticker, indexed by date."""
        table = self.load(asof=asof)
        if table.empty:
            return None
        rows = table[table["ticker"] == ticker.upper()]
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
            log.info("%s: newest insider price is %s, older than %d days", ticker, observed, max_age_days)
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
        return 0 if table.empty else int(table["ticker"].nunique())


def resolve_price(
    ticker: str,
    *,
    asof: date,
    insider: "SECInsiderPriceProvider | None",
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
            candidates.append({
                "price": price, "source": "sec_insider", "date": when,
                "non_affiliate_fraction": None,
            })

        calibrated = calibrated_price_from_float(
            float_history, shares_outstanding, insider.price_series(ticker, asof=asof)
        )
        if calibrated is not None:
            price, fraction, when = calibrated
            candidates.append({
                "price": price, "source": "public_float_calibrated", "date": when,
                "non_affiliate_fraction": fraction,
            })

    if not candidates and public_float and float_history is not None and not float_history.empty:
        implied = implied_price_from_float(public_float, shares_outstanding)
        if implied is not None:
            candidates.append({
                "price": implied, "source": "public_float_lower_bound",
                "date": pd.Timestamp(float_history.index[-1]).date(),
                "non_affiliate_fraction": None,
            })

    if not candidates:
        return None

    best = max(candidates, key=lambda c: c["date"])
    best["age_days"] = (asof - best["date"]).days
    if best["age_days"] > max_age_days:
        return None
    return best


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
                float_date, price_date, abs((float_date - price_date).days),
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
        f_day = pd.Timestamp(f_date).date()
        nearest = min(price_history.index, key=lambda d: abs((pd.Timestamp(d).date() - f_day).days))
        gap = abs((pd.Timestamp(nearest).date() - f_day).days)
        if gap > max_gap_days:
            continue
        fraction = calibrate_non_affiliate_fraction(
            float(f_value), shares_outstanding, float(price_history.loc[nearest]),
            float_date=f_day, price_date=pd.Timestamp(nearest).date(), max_gap_days=max_gap_days,
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
