"""SEC EDGAR XBRL provider — the primary fundamentals source.

Free, official, keyless, and it carries the real ``filed`` date of every fact, which makes genuine
point-in-time backtesting possible rather than approximated with a fixed reporting lag.

Four traps are handled here, each verified against live filings (docs/design/DATA-GROUND-TRUTH.md):

1. **``fy``/``fp`` describe the filing, not the fact.** AAPL's history carries a record for fiscal
   2022 stamped ``fy=2024``. Periods are derived from ``start``/``end`` only.
2. **No Q4 10-Q exists.** AAPL FY2024 revenue is 391.04B; the three available quarterly records sum
   to 296.11B. Naively summing them understates TTM by 24%. Q4 is derived as FY − Q1 − Q2 − Q3.
3. **10-Qs report year-to-date cumulatives alongside discrete quarters.** Fewer than half of AAPL's
   revenue records are discrete quarters, so records are classified by duration before use.
4. **Restatements** put several values on one period. Selection is explicit and mode-driven.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from ..types import Fundamentals, PitMode, SecuritySnapshot
from .concepts import AVERAGED_CONCEPTS, CONCEPTS, DEI_CONCEPTS, PERIOD_TYPES
from .sectors import classify_sic

log = logging.getLogger(__name__)

__all__ = ["SECClient", "SECProvider", "normalize_duration_facts", "normalize_instant_facts"]

_DAYS_PER_QUARTER = 91.31
_SEC_BASE = "https://data.sec.gov"
_SEC_WWW = "https://www.sec.gov"
_DEFAULT_CONTACT = "stock-grader (set STOCK_GRADER_CONTACT to your email)"


class _RateLimiter:
    """Token bucket. SEC asks for no more than 10 requests/second; we sit at 8 with jitter."""

    def __init__(self, rate: float = 8.0) -> None:
        self._min_interval = 1.0 / rate
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


class SECClient:
    """HTTP client for EDGAR with rate limiting and an on-disk cache.

    ``companyfacts`` payloads run 3–7 MB, so gzip is always requested and responses are cached to
    disk with a TTL.
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        contact: str | None = None,
        ttl_hours: float = 24.0,
        rate: float = 8.0,
        timeout: float = 45.0,
    ) -> None:
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "stock-grader")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.contact = contact or os.environ.get("STOCK_GRADER_CONTACT") or _DEFAULT_CONTACT
        self.ttl = timedelta(hours=ttl_hours)
        self.timeout = timeout
        self._limiter = _RateLimiter(rate)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": f"Stock-Grader/0.1 ({self.contact})",
            "Accept-Encoding": "gzip, deflate",
        })

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace(":", "_")
        return self.cache_dir / f"{safe}.json"

    def get_json(self, url: str, key: str, *, refresh: bool = False) -> dict[str, Any] | None:
        """Fetch JSON, serving from cache when fresh. Returns ``None`` on unrecoverable failure."""
        path = self._cache_path(key)
        if not refresh and path.exists():
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age < self.ttl:
                try:
                    return json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    log.warning("corrupt cache entry %s, refetching", path)

        backoff = 1.0
        for attempt in range(4):
            self._limiter.acquire()
            try:
                resp = self._session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("SEC request failed (%s): %s", url, exc)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    log.warning("SEC returned non-JSON for %s", url)
                    return None
                try:
                    path.write_text(json.dumps(payload))
                except OSError as exc:
                    log.debug("could not cache %s: %s", key, exc)
                return payload
            if resp.status_code == 404:
                log.info("SEC 404 for %s", url)
                return None
            if resp.status_code in (429, 503):
                log.info("SEC throttled (%s), backing off %.1fs", resp.status_code, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            log.warning("SEC returned HTTP %s for %s", resp.status_code, url)
            return None
        log.warning("SEC request gave up after retries: %s", url)
        return None

    def ticker_map(self, *, refresh: bool = False) -> dict[str, str]:
        """Ticker -> zero-padded 10-digit CIK."""
        payload = self.get_json(f"{_SEC_WWW}/files/company_tickers.json", "company_tickers", refresh=refresh)
        if not payload:
            return {}
        out: dict[str, str] = {}
        for entry in payload.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik = entry.get("cik_str")
            if ticker and cik is not None:
                out[ticker] = str(cik).zfill(10)
        return out

    def company_facts(self, cik: str, *, refresh: bool = False) -> dict[str, Any] | None:
        cik = str(cik).zfill(10)
        return self.get_json(f"{_SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json", f"facts_{cik}", refresh=refresh)

    def submissions(self, cik: str, *, refresh: bool = False) -> dict[str, Any] | None:
        cik = str(cik).zfill(10)
        return self.get_json(f"{_SEC_BASE}/submissions/CIK{cik}.json", f"sub_{cik}", refresh=refresh)


# --------------------------------------------------------------------------------------------
# XBRL fact normalisation
# --------------------------------------------------------------------------------------------


def _quarters_spanned(start: date, end: date) -> int:
    """How many quarters a duration fact covers: 1 (discrete), 2/3 (year-to-date), 4 (full year)."""
    return int(round((end - start).days / _DAYS_PER_QUARTER))


def _select(records: list[dict], pit_mode: PitMode, asof: date | None) -> dict | None:
    """Resolve competing vintages of one period to a single value.

    ``PIT`` keeps the latest filing that was public on ``asof`` — what an investor could have known.
    ``LATEST`` keeps the most recent filing outright — the most-restated, most-accurate figure.
    """
    if not records:
        return None
    if pit_mode is PitMode.PIT and asof is not None:
        eligible = [r for r in records if _parse(r.get("filed")) and _parse(r["filed"]) <= asof]
        if not eligible:
            return None
        return max(eligible, key=lambda r: r["filed"])
    return max(records, key=lambda r: r.get("filed", ""))


def _parse(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _usd_records(fact: dict[str, Any]) -> list[dict]:
    """Pull the USD (or USD-per-share, or plain-share) unit series out of a fact."""
    units = fact.get("units", {})
    for key in ("USD", "USD/shares", "shares", "pure"):
        if key in units:
            return list(units[key])
    return list(next(iter(units.values()), []))


def normalize_duration_facts(
    fact: dict[str, Any],
    *,
    pit_mode: PitMode = PitMode.LATEST,
    asof: date | None = None,
    averaged: bool = False,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Turn a duration fact into (discrete quarters, fiscal years, filing dates).

    Discrete quarters are assembled in three passes, in order of trustworthiness:

    1. Records that already span one quarter.
    2. Year-to-date records differenced against each other (``Q3 = YTD3 − YTD2``) where a discrete
       record is absent.
    3. The **derived Q4**: no US filer publishes a fourth-quarter 10-Q, so wherever a fiscal-year
       record is present and exactly three of its quarters are known, the fourth is recovered as
       ``FY − Q1 − Q2 − Q3``. Skipping this step understates every trailing-twelve-month flow.

    Args:
        averaged: set for facts that are **weighted averages rather than sums**, above all the
            diluted and basic share counts. Passes 2 and 3 are arithmetic on flows and are simply
            wrong for an average: subtracting three quarterly average share counts from the annual
            average produced a share count of *negative 30 billion* for Apple, which then flowed
            into market cap and silently corrupted every valuation multiple. For these concepts
            only genuinely-reported discrete quarters are kept.
    """
    records = _usd_records(fact)
    by_period: dict[tuple[date, date], list[dict]] = {}
    for rec in records:
        start, end = _parse(rec.get("start")), _parse(rec.get("end"))
        if start is None or end is None:
            continue
        by_period.setdefault((start, end), []).append(rec)

    chosen: dict[tuple[date, date], dict] = {}
    for period, recs in by_period.items():
        pick = _select(recs, pit_mode, asof)
        if pick is not None:
            chosen[period] = pick

    quarters: dict[date, float] = {}
    filings: dict[date, date] = {}
    years: dict[date, float] = {}
    ytd: dict[tuple[date, int], tuple[date, float]] = {}

    # Pass 1 + fiscal years.
    for (start, end), rec in sorted(chosen.items()):
        n_q = _quarters_spanned(start, end)
        value = rec.get("val")
        if value is None:
            continue
        if n_q == 1:
            quarters[end] = float(value)
            filed = _parse(rec.get("filed"))
            if filed:
                filings[end] = filed
        elif n_q == 4:
            years[end] = float(value)
            filed = _parse(rec.get("filed"))
            if filed:
                filings.setdefault(end, filed)
        elif n_q in (2, 3):
            ytd[(start, n_q)] = (end, float(value))

    if averaged:
        # Averages do not decompose by subtraction; report only what was actually filed.
        return (
            pd.Series(quarters, dtype="float64").sort_index(),
            pd.Series(years, dtype="float64").sort_index(),
            pd.Series(filings, dtype="object").sort_index(),
        )

    # Pass 2: difference year-to-date records where the discrete quarter is missing.
    for (start, n_q), (end, value) in sorted(ytd.items()):
        if end in quarters:
            continue
        prior = ytd.get((start, n_q - 1))
        prior_value = prior[1] if prior else (quarters.get(_prev_quarter_end(start, quarters)) if n_q == 2 else None)
        if n_q == 2 and prior is None:
            # YTD2 minus the single Q1 that shares this fiscal start.
            q1 = next((v for e, v in sorted(quarters.items()) if start <= e <= end), None)
            prior_value = q1
        if prior_value is not None:
            quarters[end] = value - prior_value

    # Pass 3: derive the missing Q4 from the fiscal-year total.
    for fy_end, fy_value in sorted(years.items()):
        fy_start = _fiscal_start(fy_end, chosen)
        if fy_start is None:
            continue
        inside = sorted(e for e in quarters if fy_start <= e <= fy_end)
        if len(inside) == 3 and fy_end not in quarters:
            quarters[fy_end] = fy_value - sum(quarters[e] for e in inside)

    q_series = pd.Series(quarters, dtype="float64").sort_index()
    a_series = pd.Series(years, dtype="float64").sort_index()
    f_series = pd.Series(filings, dtype="object").sort_index()
    return q_series, a_series, f_series


def _prev_quarter_end(start: date, quarters: dict[date, float]) -> date | None:
    candidates = [e for e in quarters if e >= start]
    return min(candidates) if candidates else None


def _fiscal_start(fy_end: date, chosen: dict[tuple[date, date], dict]) -> date | None:
    """The start date of the fiscal-year record ending on ``fy_end``."""
    for (start, end) in chosen:
        if end == fy_end and _quarters_spanned(start, end) == 4:
            return start
    return None


def normalize_instant_facts(
    fact: dict[str, Any],
    *,
    pit_mode: PitMode = PitMode.LATEST,
    asof: date | None = None,
) -> pd.Series:
    """Turn a balance-sheet fact into a series indexed by observation date.

    Instants need no quarter derivation — they are already point observations — but they still need
    restatement resolution.
    """
    records = _usd_records(fact)
    by_date: dict[date, list[dict]] = {}
    for rec in records:
        if rec.get("start") is not None:
            continue  # a duration fact hiding in an instant concept
        end = _parse(rec.get("end"))
        if end is None:
            continue
        by_date.setdefault(end, []).append(rec)

    out: dict[date, float] = {}
    for end, recs in by_date.items():
        pick = _select(recs, pit_mode, asof)
        if pick is not None and pick.get("val") is not None:
            out[end] = float(pick["val"])
    return pd.Series(out, dtype="float64").sort_index()


def build_fundamentals(
    facts: dict[str, Any],
    *,
    pit_mode: PitMode = PitMode.LATEST,
    asof: date | None = None,
) -> Fundamentals:
    """Assemble normalised quarterly and annual statements from a ``companyfacts`` payload."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    quarterly: dict[str, pd.Series] = {}
    annual: dict[str, pd.Series] = {}
    filings: dict[date, date] = {}
    tag_used: dict[str, str] = {}

    for concept, chain in CONCEPTS.items():
        tag = next((t for t in chain if t in gaap), None)
        if tag is None:
            continue
        tag_used[concept] = tag
        fact = gaap[tag]
        if PERIOD_TYPES[concept].value == "instant":
            series = normalize_instant_facts(fact, pit_mode=pit_mode, asof=asof)
            if not series.empty:
                quarterly[concept] = series
                annual[concept] = series
        else:
            q, a, f = normalize_duration_facts(
                fact, pit_mode=pit_mode, asof=asof, averaged=concept in AVERAGED_CONCEPTS
            )
            if not q.empty:
                quarterly[concept] = q
            if not a.empty:
                annual[concept] = a
            for idx, val in f.items():
                filings.setdefault(idx, val)

    q_df = pd.DataFrame(quarterly).sort_index() if quarterly else pd.DataFrame()
    a_df = pd.DataFrame(annual).sort_index() if annual else pd.DataFrame()

    # Derived concepts that filers often omit but which follow arithmetically.
    _derive(q_df)
    _derive(a_df)

    return Fundamentals(
        quarterly=q_df,
        annual=a_df,
        filed=pd.Series(filings, dtype="object").sort_index(),
        period_type=dict(PERIOD_TYPES),
        tag_used=tag_used,
        pit_mode=pit_mode,
        averaged=set(AVERAGED_CONCEPTS),
    )


def _derive(df: pd.DataFrame) -> None:
    """Fill in concepts that are arithmetic consequences of others, in place.

    Gross profit is absent from 5 of 8 sampled filers' XBRL but is simply revenue − COGS wherever
    both exist. Where neither is available the concept stays absent, which the metric layer reports
    as missing rather than inventing a zero.
    """
    if df.empty:
        return
    if "gross_profit" not in df and {"revenue", "cogs"} <= set(df.columns):
        df["gross_profit"] = df["revenue"] - df["cogs"]
    if "total_debt" not in df:
        parts = [c for c in ("long_term_debt", "short_term_debt") if c in df.columns]
        if parts:
            df["total_debt"] = df[parts].sum(axis=1, min_count=1)
    if "net_debt" not in df and "total_debt" in df and "cash" in df:
        df["net_debt"] = df["total_debt"] - df["cash"]
    if "ebit" not in df and "operating_income" in df:
        df["ebit"] = df["operating_income"]
    elif "ebit" not in df and {"pretax_income", "interest_expense"} <= set(df.columns):
        df["ebit"] = df["pretax_income"] + df["interest_expense"]
    if "ebitda" not in df and {"ebit", "depreciation_amortization"} <= set(df.columns):
        df["ebitda"] = df["ebit"] + df["depreciation_amortization"]
    if "fcf" not in df and {"cfo", "capex"} <= set(df.columns):
        # capex is reported as a positive outflow in the cash-flow statement.
        df["fcf"] = df["cfo"] - df["capex"].abs()
    if "working_capital" not in df and {"current_assets", "current_liabilities"} <= set(df.columns):
        df["working_capital"] = df["current_assets"] - df["current_liabilities"]
    if "tangible_book" not in df and "equity" in df:
        intangible_cols = [c for c in ("goodwill", "intangibles") if c in df.columns]
        if intangible_cols:
            df["tangible_book"] = df["equity"] - df[intangible_cols].sum(axis=1, min_count=1)
        else:
            df["tangible_book"] = df["equity"]
    if "invested_capital" not in df and {"equity", "total_debt"} <= set(df.columns):
        ic = df["equity"] + df["total_debt"]
        if "cash" in df.columns:
            ic = ic - df["cash"]
        df["invested_capital"] = ic


class SECProvider:
    """Fundamentals provider backed by SEC EDGAR. Prices, if any, come from elsewhere."""

    name = "sec"
    provides_prices = False
    provides_fundamentals = True

    def __init__(self, client: SECClient | None = None) -> None:
        self.client = client or SECClient()
        self._tickers: dict[str, str] | None = None

    def resolve_cik(self, ticker: str) -> str | None:
        if self._tickers is None:
            self._tickers = self.client.ticker_map()
        return self._tickers.get(ticker.upper())

    def fetch(
        self,
        ticker: str,
        *,
        asof: date | None = None,
        pit_mode: PitMode = PitMode.LATEST,
        refresh: bool = False,
    ) -> SecuritySnapshot:
        """Build a snapshot with fundamentals, sector and share count. Never raises on data gaps."""
        asof = asof or date.today()
        snap = SecuritySnapshot(ticker=ticker.upper(), asof=asof)

        cik = self.resolve_cik(ticker)
        if cik is None:
            snap.warnings.append(f"{ticker}: no CIK found in SEC ticker map (non-US or delisted?)")
            return snap
        snap.cik = cik

        subs = self.client.submissions(cik, refresh=refresh)
        if subs:
            snap.name = subs.get("name")
            snap.sic = subs.get("sic")
            snap.industry = subs.get("sicDescription")
            snap.sector = classify_sic(subs.get("sic"))

        facts = self.client.company_facts(cik, refresh=refresh)
        if not facts:
            snap.warnings.append(f"{ticker}: SEC companyfacts unavailable")
            return snap

        snap.fundamentals = build_fundamentals(facts, pit_mode=pit_mode, asof=asof)
        snap.meta["pit_mode"] = pit_mode.value
        snap.meta["tags_used"] = snap.fundamentals.tag_used

        dei = facts.get("facts", {}).get("dei", {})
        for concept, chain in DEI_CONCEPTS.items():
            tag = next((t for t in chain if t in dei), None)
            if tag is None:
                continue
            series = normalize_instant_facts(dei[tag], pit_mode=pit_mode, asof=asof)
            if series.empty:
                continue
            if concept == "shares_outstanding":
                snap.shares_outstanding = float(series.iloc[-1])
            elif concept == "public_float":
                snap.public_float = float(series.iloc[-1])
                # The full dated history, not just the latest value: pricing from public float
                # needs a float and a market price measured on the *same* date to solve the
                # affiliate share, and only the history offers a choice of dates to match against.
                snap.meta["public_float_history"] = series

        # Diluted share count from the income statement is the better denominator for per-share
        # work when it is available; the dei cover-page count is a fallback.
        if snap.fundamentals is not None:
            diluted = snap.fundamentals.latest("shares_diluted")
            if diluted:
                snap.meta["shares_diluted"] = diluted
                if snap.shares_outstanding is None:
                    snap.shares_outstanding = diluted

        return snap
