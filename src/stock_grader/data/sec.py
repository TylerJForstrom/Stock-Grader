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
from typing import Any, Protocol

import numpy as np
import pandas as pd
import requests

from ..types import Fundamentals, PitMode, SecuritySnapshot
from .cache import default_cache_dir, safe_cache_path
from .concepts import AVERAGED_CONCEPTS, DEI_CONCEPTS, PERIOD_TYPES, chains_for
from .sectors import classify_sic
from .symbols import ticker_variants

log = logging.getLogger(__name__)

__all__ = [
    "SECClient",
    "SECProvider",
    "normalize_duration_facts",
    "normalize_instant_facts",
    "restate_for_splits",
]

_DAYS_PER_QUARTER = 91.31
_SEC_BASE = "https://data.sec.gov"
_SEC_WWW = "https://www.sec.gov"
_DEFAULT_CONTACT = "stock-grader (set STOCK_GRADER_CONTACT to your email)"
# Matches metrics.fundamental.MAX_BALANCE_AGE_DAYS; duplicated to avoid a circular import.
_MAX_FACT_AGE_DAYS = 400
# Consecutive whole-request failures before the client stops trying for the rest of the run.
_CIRCUIT_BREAKER_THRESHOLD = 3


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
        offline: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir or default_cache_dir())
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.contact = contact or os.environ.get("STOCK_GRADER_CONTACT") or _DEFAULT_CONTACT
        self.ttl = timedelta(hours=ttl_hours)
        self.timeout = timeout
        # --no-network gated the price providers but never reached here, so a "cache only" run
        # still fetched from SEC — and a cache one second past its TTL was discarded rather than
        # served, which is exactly backwards when the user has asked not to use the network.
        self.offline = offline
        self._consecutive_failures = 0
        self._limiter = _RateLimiter(rate)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": f"Stock-Grader/0.1 ({self.contact})",
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def _cache_path(self, key: str) -> Path:
        return safe_cache_path(self.cache_dir, "", key, ".json")

    def _read_stale(self, path: Path, key: str) -> dict[str, Any] | None:
        """Last resort after online failure: an expired cached copy beats nothing."""
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        log.warning("SEC unreachable; serving STALE cached copy of %s", key)
        return payload

    def get_json(self, url: str, key: str, *, refresh: bool = False) -> dict[str, Any] | None:
        """Fetch JSON, serving from cache when fresh. Returns ``None`` on unrecoverable failure."""
        path = self._cache_path(key)
        if not refresh and path.exists():
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age < self.ttl or self.offline:
                try:
                    return json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    log.warning("corrupt cache entry %s, refetching", path)
        if self.offline:
            log.info("offline: no cached copy of %s", key)
            return None

        # Circuit breaker. Without it, a network outage cost 30 seconds of retry-sleeping per
        # ticker — over 40 minutes for the default universe — and the command still exited 0.
        if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            log.warning(
                "SEC unreachable after %d consecutive failures; not retrying",
                self._consecutive_failures,
            )
            return self._read_stale(path, key)

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
                self._consecutive_failures = 0
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
            return self._read_stale(path, key)
        self._consecutive_failures += 1
        log.warning("SEC request gave up after retries: %s", url)
        return self._read_stale(path, key)

    def get_bytes_with_headers(self, url: str) -> tuple[bytes, dict[str, str]] | None:
        """Rate-limited, retrying binary GET for sec.gov bulk files (dataset zips).

        Shares the client's limiter, session (declared User-Agent), and circuit
        breaker so every request to SEC hosts observes one fair-access budget.
        No disk caching here — callers cache their own derived artifacts.
        """
        if self.offline:
            return None
        if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            log.warning(
                "SEC unreachable after %d consecutive failures; not retrying",
                self._consecutive_failures,
            )
            return None
        backoff = 1.0
        for attempt in range(4):
            self._limiter.acquire()
            try:
                resp = self._session.get(
                    url,
                    timeout=max(self.timeout, 180.0),
                    headers={"Accept-Encoding": "identity"},
                )
            except requests.RequestException as exc:
                log.warning("SEC request failed (%s): %s", url, exc)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 200:
                self._consecutive_failures = 0
                headers = {str(key): str(value) for key, value in resp.headers.items()}
                return resp.content, headers
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
        self._consecutive_failures += 1
        log.warning("SEC request gave up after retries: %s", url)
        return None

    def get_bytes(self, url: str) -> bytes | None:
        """Fetch binary content while discarding representation headers."""
        response = self.get_bytes_with_headers(url)
        return None if response is None else response[0]

    def head(self, url: str) -> dict[str, str] | None:
        """Issue a rate-limited HEAD through the shared fair-access client."""
        if self.offline:
            return None
        if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            log.warning(
                "SEC unreachable after %d consecutive failures; not retrying",
                self._consecutive_failures,
            )
            return None
        backoff = 1.0
        for attempt in range(4):
            self._limiter.acquire()
            try:
                response = self._session.head(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    headers={"Accept-Encoding": "identity"},
                )
            except requests.RequestException as exc:
                log.warning("SEC HEAD failed (%s): %s", url, exc)
                time.sleep(backoff)
                backoff *= 2
                continue
            if response.status_code == 200:
                self._consecutive_failures = 0
                return {str(key): str(value) for key, value in response.headers.items()}
            if response.status_code == 404:
                log.info("SEC HEAD 404 for %s", url)
                return None
            if response.status_code in (429, 503):
                log.info(
                    "SEC throttled HEAD (%s), backing off %.1fs",
                    response.status_code,
                    backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            log.warning("SEC returned HTTP %s for HEAD %s", response.status_code, url)
            return None
        self._consecutive_failures += 1
        log.warning("SEC HEAD gave up after retries: %s", url)
        return None

    def ticker_map(self, *, refresh: bool = False) -> dict[str, str]:
        """Ticker -> zero-padded 10-digit CIK."""
        payload = self.get_json(
            f"{_SEC_WWW}/files/company_tickers.json", "company_tickers", refresh=refresh
        )
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
        return self.get_json(
            f"{_SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json", f"facts_{cik}", refresh=refresh
        )

    def submissions(self, cik: str, *, refresh: bool = False) -> dict[str, Any] | None:
        cik = str(cik).zfill(10)
        return self.get_json(
            f"{_SEC_BASE}/submissions/CIK{cik}.json", f"sub_{cik}", refresh=refresh
        )


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


# Units that are either dollars or dimensionless. Anything else is another currency and must not
# be mixed with them.
_ACCEPTED_UNITS = ("USD", "USD/shares", "shares", "pure")


def _usd_records(fact: dict[str, Any]) -> list[dict]:
    """Pull the USD or dimensionless unit series out of a fact.

    Returns **empty** for a fact reported only in a foreign currency, rather than falling back to
    whatever unit happens to be present. Foreign private issuers file with the SEC in their own
    currency: Toyota reports revenue in JPY and Alibaba reports receivables in CNY. Silently
    treating ~48 trillion yen as dollars overstates Toyota's revenue by roughly 150x and makes it
    look extraordinarily cheap on every sales multiple; mixing CNY receivables against USD assets
    produces a ratio of two different currencies.

    Dropping the fact means the metric reports MISSING, which is the honest outcome — converting
    would need a point-in-time FX rate for the fact's own period, which is a larger piece of work
    than this guard.
    """
    units = fact.get("units", {})
    for key in _ACCEPTED_UNITS:
        if key in units:
            return list(units[key])
    return []


def detect_currencies(facts: dict[str, Any]) -> set[str]:
    """Currency units appearing anywhere in a companyfacts payload."""
    found: set[str] = set()
    for taxonomy in facts.get("facts", {}).values():
        for fact in taxonomy.values():
            for unit in fact.get("units", {}):
                base = unit.split("/")[0]
                if len(base) == 3 and base.isalpha() and base.isupper():
                    found.add(base)
    return found


def normalize_duration_facts(
    fact: dict[str, Any],
    *,
    pit_mode: PitMode = PitMode.LATEST,
    asof: date | None = None,
    averaged: bool = False,
    concept: str | None = None,
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
            if concept in SIGN_CONSTRAINED and float(value) < 0:
                # Filers make sign errors too. Chevron filed capex of -$4.452B for 2008-Q1 (2 of
                # 160 records), which no derivation gate can catch because it is what the company
                # actually reported. Cash paid out is not negative.
                log.info("%s %s: dropping filed negative value %.4g", concept or "fact", end, value)
                continue
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
        prior_value = (
            prior[1]
            if prior
            else (quarters.get(_prev_quarter_end(start, quarters)) if n_q == 2 else None)
        )
        if n_q == 2 and prior is None:
            # YTD2 minus the single Q1 that shares this fiscal start.
            q1 = next((v for e, v in sorted(quarters.items()) if start <= e <= end), None)
            prior_value = q1
        if prior_value is not None:
            # Same vintage hazard as the Q4 derivation: two year-to-date records selected
            # independently can come from different restatements, so their difference is not a
            # quarter. Chevron's 2008-Q1 capex came out at -$4.45B this way, between siblings of
            # +$13.4B and +$4.7B.
            differenced = value - prior_value
            if _plausible_q4(concept, differenced, list(quarters.values())[-3:]):
                quarters[end] = differenced
            else:
                log.info(
                    "%s %s: rejecting year-to-date difference of %.4g",
                    concept or "fact",
                    end,
                    differenced,
                )

    # Pass 3: derive the missing Q4 from the fiscal-year total.
    for fy_end, fy_value in sorted(years.items()):
        fy_start = _fiscal_start(fy_end, chosen)
        if fy_start is None:
            continue
        inside = sorted(e for e in quarters if fy_start <= e <= fy_end)
        if len(inside) == 3 and fy_end not in quarters:
            siblings = [quarters[e] for e in inside]
            derived = fy_value - sum(siblings)
            if _plausible_q4(concept, derived, siblings):
                quarters[fy_end] = derived
            else:
                log.info(
                    "%s %s: rejecting derived Q4 of %.4g against sibling quarters %s",
                    concept or "fact",
                    fy_end,
                    derived,
                    [f"{v:.4g}" for v in siblings],
                )

    q_series = pd.Series(quarters, dtype="float64").sort_index()
    a_series = pd.Series(years, dtype="float64").sort_index()
    f_series = pd.Series(filings, dtype="object").sort_index()
    return q_series, a_series, f_series


def _prev_quarter_end(start: date, quarters: dict[date, float]) -> date | None:
    candidates = [e for e in quarters if e >= start]
    return min(candidates) if candidates else None


def _fiscal_start(fy_end: date, chosen: dict[tuple[date, date], dict]) -> date | None:
    """The start date of the fiscal-year record ending on ``fy_end``."""
    for start, end in chosen:
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


# Concepts that cannot legitimately be negative in a single quarter — cash paid out, share counts,
# revenue, and depreciation. A negative here is a filer error or a failed derivation, never a
# business event.
#
# Deliberately excludes income_tax, which is genuinely negative when a loss produces a tax benefit
# (measured across 20 filers, that was the overwhelming majority of apparent "violations"), and
# excludes operating_income, net_income and pretax_income, which are negative whenever a company
# loses money. Constraining those would delete real losses — precisely the companies a solvency
# pillar most needs to see.
SIGN_CONSTRAINED = frozenset(
    {
        "revenue",
        "cogs",
        "capex",
        "depreciation_amortization",
        "shares_basic",
        "shares_diluted",
        "buybacks",
        "dividends_paid",
    }
)


def _plausible_q4(concept: str | None, derived: float, siblings: list[float]) -> bool:
    """Whether a Q4 derived as FY minus three quarters can be believed.

    The fiscal-year total and the three quarters are each selected independently, so they can come
    from **different restatement vintages** and the subtraction then mixes them. Target's FY2013
    capex was filed at $3.453B and later restated to $1.886B while the year-to-date Q3 figure stayed
    at $2.839B in both vintages — deriving Q4 from the restated year and the original quarters gave
    **-$953M**, the only negative in the series, which an ``.abs()`` downstream quietly turned into
    a plausible $953M outflow inside free cash flow.

    Two checks, both on the value rather than on filing dates: filing dates do not catch Target,
    whose winning fiscal-year record was filed *after* all four quarters.
    """
    if not np.isfinite(derived):
        return False
    if concept in SIGN_CONSTRAINED and derived < 0:
        return False
    magnitudes = [abs(v) for v in siblings if np.isfinite(v)]
    if not magnitudes:
        return True
    median = float(np.median(magnitudes))
    if median <= 0:
        return True
    # A real fourth quarter looks like its siblings. Wide bounds on purpose: seasonal businesses do
    # earn several times a quiet quarter, so this catches arithmetic failure, not seasonality.
    return 0.15 * median <= abs(derived) <= 6.0 * median


# Ratios a stock split plausibly takes. A jump close to one of these, with the balance sheet
# unchanged, is a split rather than a financing event.
# Capped at 20:1, the largest split any major US company has done — Amazon and Alphabet both did
# exactly that in 2022. Allowing 25 or 30 let Lucid's 29.8x SPAC reverse-merger read as a split.
_SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0)


def _looks_like_split(ratio: float, tolerance: float = 0.06) -> float | None:
    """The split factor this ratio represents, or ``None`` if it is not close to one."""
    if not np.isfinite(ratio) or ratio <= 0:
        return None
    for candidate in _SPLIT_RATIOS:
        if abs(ratio / candidate - 1.0) <= tolerance:
            return candidate
        if abs(ratio * candidate - 1.0) <= tolerance:  # reverse split
            return 1.0 / candidate
    return None


def restate_for_splits(shares: pd.Series, scale_reference: pd.Series | None = None) -> pd.Series:
    """Put a share-count series onto one post-split basis.

    Filers restate share counts retroactively when they split, but only in filings made *after* the
    split. Selecting the newest vintage per period therefore stitches restated recent periods onto
    un-restated older ones, and the seam looks like an enormous share issuance: NVIDIA's annual
    series jumps **9.9x** at its 10-for-1 split, Amazon's **20.2x**, Apple's **3.8x**. Any metric
    reading several years of share history across that seam sees a split as dilution.

    A split changes the share count and nothing else, so ``scale_reference`` — a balance-sheet or
    income item from the same periods — distinguishes the two cases. If shares jump tenfold while
    assets grow at an ordinary pace, it is a split; if assets move roughly *with* the shares, money
    came in and it is an issuance.

    **This is inference, not a corporate-actions feed**, and it has a known limit. Measured against
    five companies that never split, four are now left alone (Carvana's equity raise, Rivian's and
    Snowflake's IPOs, Uber's IPO); Lucid still trips it, because a 0.12x share ratio after its SPAC
    recapitalisation is arithmetically indistinguishable from a 1-for-8 reverse split. Every
    inferred split is logged at INFO so it can be audited, and the candidate ratios stop at 20:1 —
    the largest split any major US company has done, Amazon and Alphabet in 2022.
    """
    clean = shares.dropna()
    if len(clean) < 2:
        return shares
    factors = pd.Series(1.0, index=clean.index)
    cumulative = 1.0
    # Walk backwards from the newest observation, which is always on the current basis.
    for i in range(len(clean) - 1, 0, -1):
        ratio = float(clean.iloc[i] / clean.iloc[i - 1]) if clean.iloc[i - 1] else float("nan")
        split = _looks_like_split(ratio)
        if split is not None:
            # A split changes the share count and leaves the balance sheet ALONE. So the test is
            # that the reference is roughly FLAT — not that it moved with the shares, which was
            # the original mistake: an IPO or a SPAC merger moves both, just not proportionally,
            # so a "did they move together" check let them through. Measured fabrications under
            # that rule: Carvana (a 1.99x equity raise), Rivian and Snowflake (IPOs) and Lucid
            # (a 29.8x SPAC merger) were all restated as splits.
            #
            # Without a reference series there is no way to tell a split from an issuance, so the
            # series is left alone rather than guessed at.
            if scale_reference is None:
                split = None
            else:
                reference = scale_reference.dropna()
                try:
                    now = float(reference.asof(clean.index[i]))
                    before = float(reference.asof(clean.index[i - 1]))
                except (KeyError, TypeError, ValueError):
                    before = now = 0.0
                if not before or not np.isfinite(now / before):
                    split = None
                else:
                    # Compare how far each moved. A split multiplies shares while the balance
                    # sheet carries on at ordinary growth, so assets fall far short of the share
                    # change. An issuance raises money, so assets move roughly *with* the shares.
                    #
                    # Requiring assets to be flat outright was too strict — a split happens
                    # mid-year and assets still grow 10-20% over that year, which cost NVIDIA and
                    # Apple their real corrections. Requiring assets to keep pace was too loose
                    # and fabricated splits for Carvana, Rivian, Snowflake and Lucid. The ratio of
                    # ratios separates them cleanly in both directions.
                    asset_ratio = now / before
                    share_ratio = ratio if ratio >= 1.0 else 1.0 / ratio
                    asset_move = asset_ratio if asset_ratio >= 1.0 else 1.0 / asset_ratio
                    if asset_move > 0.6 * share_ratio:
                        split = None  # the business changed size with the count: issuance
        if split is not None:
            cumulative *= split
            log.info(
                "inferred a %.3gx split at %s (share ratio %.3g); earlier periods restated",
                split,
                clean.index[i],
                ratio,
            )
        factors.iloc[i - 1] = cumulative
    return (clean * factors).reindex(shares.index)


def _annualise_instants(series: pd.Series) -> pd.Series:
    """Reduce a quarterly balance-sheet series to one observation per fiscal year.

    Keeps the **last** observation within each fiscal year, anchored on the month of the most
    recent observation so that non-calendar fiscal years (Apple's September, Walmart's January) are
    grouped correctly rather than split across calendar boundaries.
    """
    if series.empty:
        return series
    index = pd.to_datetime(pd.Series(list(series.index)))
    anchor_month = int(index.iloc[-1].month)
    # Shift the year boundary so it falls just after the fiscal year end.
    shifted = index - pd.DateOffset(months=anchor_month % 12)
    grouped = pd.Series(series.to_numpy(), index=series.index).groupby(shifted.dt.year.to_numpy())
    return grouped.last().set_axis(grouped.apply(lambda s: s.index[-1])).sort_index()


def _latest_end(
    fact: dict[str, Any],
    *,
    pit_mode: PitMode = PitMode.LATEST,
    asof: date | None = None,
) -> date | None:
    """Newest eligible period end across a fact's records.

    Under PIT, tag selection must see the same information set as value selection.  Looking at a
    tag's records through today before filtering its values let a tag adopted years later displace
    the tag investors actually saw on the historical date.
    """
    records = _usd_records(fact)
    if pit_mode is PitMode.PIT and asof is not None:
        records = [
            record
            for record in records
            if (filed := _parse(record.get("filed"))) is not None and filed <= asof
        ]
    ends = [_parse(record.get("end")) for record in records]
    ends = [e for e in ends if e is not None]
    return max(ends) if ends else None


def _select_tag(
    chain: tuple[str, ...],
    gaap: dict[str, Any],
    *,
    stale_days: int = 550,
    pit_mode: PitMode = PitMode.LATEST,
    asof: date | None = None,
) -> str | None:
    """Pick the preferred tag that still carries current data.

    Taking the first tag that merely *exists* is wrong, because filers abandon tags without
    removing their history. Lowe's still carries six ``LongTermDebtNoncurrent`` records, but the
    series stops in **2009** at $4.524B; the company's actual long-term debt sits in
    ``LongTermDebtAndCapitalLeaseObligations`` at $36.751B through 2026. Preferring chain order
    alone read Lowe's as 92% debt-free — ``debt_to_assets`` 0.082 against a true ~0.68 — and every
    solvency metric inherited it.

    So chain order still decides *preference*, but only among tags whose data is roughly as recent
    as the best available for that concept. ``stale_days`` is deliberately generous: an annual-only
    filer's balance sheet is legitimately up to 15 months old, and this must reject abandoned tags,
    not merely infrequent ones.
    """
    present = [t for t in chain if t in gaap]
    if not present:
        return None
    ends = {tag: _latest_end(gaap[tag], pit_mode=pit_mode, asof=asof) for tag in present}
    if pit_mode is PitMode.PIT and asof is not None:
        # A tag with no record filed by the grading date did not exist in the investor's
        # information set.  It must not win merely because it leads the modern preference chain.
        present = [tag for tag in present if ends[tag] is not None]
        if not present:
            return None
    dated = {t: e for t, e in ends.items() if e is not None}
    if not dated:
        return present[0]
    newest = max(dated.values())
    cutoff = newest - timedelta(days=stale_days)
    current = [t for t in present if dated.get(t) is not None and dated[t] >= cutoff]
    return current[0] if current else present[0]


def build_fundamentals(
    facts: dict[str, Any],
    *,
    pit_mode: PitMode = PitMode.LATEST,
    asof: date | None = None,
    sector: str | None = None,
) -> Fundamentals:
    """Assemble normalised quarterly and annual statements from a ``companyfacts`` payload.

    Args:
        sector: business-model class. Some concepts are measured on different bases by business
            model rather than merely tagged differently — bank revenue resolved to gross, net-of-
            interest and fee-only bases simultaneously across six large banks — so the chains are
            specialised where that matters.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    concept_chains = chains_for(sector)
    quarterly: dict[str, pd.Series] = {}
    annual: dict[str, pd.Series] = {}
    filings: dict[date, date] = {}
    tag_used: dict[str, str] = {}
    concept_provenance: dict[str, dict] = {}

    for concept, chain in concept_chains.items():
        tag = _select_tag(chain, gaap, pit_mode=pit_mode, asof=asof)
        if tag is None:
            continue
        tag_used[concept] = tag
        fact = gaap[tag]
        unit_key = next(iter(fact.get("units", {})), None)
        prov_items = fact.get("units", {}).get(unit_key, []) if unit_key else []
        if pit_mode is PitMode.PIT and asof is not None:
            cutoff = asof.isoformat()
            prov_items = [i for i in prov_items if str(i.get("filed", "")) <= cutoff]
        concept_provenance[concept] = {
            "tag": tag,
            "unit": unit_key,
            "latest_period_end": max((str(i.get("end", "")) for i in prov_items), default=None),
            "latest_filed": max((str(i.get("filed", "")) for i in prov_items), default=None),
        }
        if PERIOD_TYPES[concept].value == "instant":
            series = normalize_instant_facts(fact, pit_mode=pit_mode, asof=asof)
            if not series.empty:
                quarterly[concept] = series
                # Assigning the raw instant series to `annual` as well was silently wrong: it is a
                # quarterly series, so history(concept, 6, annual=True) returned six *quarters*.
                # book_value_cagr_5y was computing growth over 1.25 years and reporting it as a
                # five-year CAGR for every company in the universe.
                annual[concept] = _annualise_instants(series)
        else:
            q, a, f = normalize_duration_facts(
                fact,
                pit_mode=pit_mode,
                asof=asof,
                averaged=concept in AVERAGED_CONCEPTS,
                concept=concept,
            )
            if not q.empty:
                quarterly[concept] = q
            if not a.empty:
                annual[concept] = a
            for idx, val in f.items():
                filings.setdefault(idx, val)

    q_df = pd.DataFrame(quarterly).sort_index() if quarterly else pd.DataFrame()
    a_df = pd.DataFrame(annual).sort_index() if annual else pd.DataFrame()

    # Put share counts on one post-split basis before anything reads a multi-year history from
    # them. Assets are the scale reference: a split moves shares and leaves the balance sheet alone.
    for frame in (q_df, a_df):
        if frame.empty:
            continue
        reference = frame["assets"] if "assets" in frame.columns else None
        for concept in ("shares_diluted", "shares_basic"):
            if concept in frame.columns:
                frame[concept] = restate_for_splits(frame[concept], reference)

    # Derived concepts that filers often omit but which follow arithmetically.
    _derive(q_df)
    _derive(a_df)

    return Fundamentals(
        quarterly=q_df,
        annual=a_df,
        filed=pd.Series(filings, dtype="object").sort_index(),
        period_type=dict(PERIOD_TYPES),
        tag_used=tag_used,
        concept_provenance=concept_provenance,
        pit_mode=pit_mode,
        averaged=set(AVERAGED_CONCEPTS),
    )


def _fill(df: pd.DataFrame, concept: str, values: pd.Series) -> None:
    """Fill a derived concept, keeping any filed value that is already present.

    Fills **gaps**, not just absent columns. A ``concept not in df`` guard is not enough, because a
    filer can abandon a tag without deleting its history: Costco, Amazon and Target all still carry
    a ``GrossProfit`` column whose last observation is 2,519, 6,142 and 3,094 days old. The column
    exists, so the derivation never ran, and gross margin was either stale-era or missing for every
    one of them — while revenue minus COGS was available and current the whole time.
    """
    if concept in df.columns:
        df[concept] = df[concept].combine_first(values)
    else:
        df[concept] = values


def _derive(df: pd.DataFrame) -> None:
    """Fill in concepts that are arithmetic consequences of others, in place.

    Gross profit is absent from 5 of 8 sampled filers' XBRL but is simply revenue − COGS wherever
    both exist. Where neither is available the concept stays absent, which the metric layer reports
    as missing rather than inventing a zero.
    """
    if df.empty:
        return
    if {"revenue", "cogs"} <= set(df.columns):
        _fill(df, "gross_profit", df["revenue"] - df["cogs"])
    if "loan_loss_allowance" not in df and {"loans_gross", "loans"} <= set(df.columns):
        # Under CECL the allowance is the gap between gross and net loans. Banks tag both sides
        # but stopped tagging the allowance itself around 2021, so deriving it keeps the credit
        # metrics alive on current data instead of resolving to a figure four years old.
        allowance = df["loans_gross"] - df["loans"]
        df["loan_loss_allowance"] = allowance.where(allowance > 0)
    if {"assets", "equity"} <= set(df.columns):
        # Walmart, Nike, TJX, McDonald's, Target and AbbVie never tag `Liabilities` at all, so both
        # ohlson_o_score and altman_z_prime returned None for them while the grade was still issued
        # — silently missing its solvency input.
        #
        # Derived as assets - equity, which reproduces the reported figure essentially exactly for
        # filers that publish both. Deliberately NOT taken from LiabilitiesAndStockholdersEquity:
        # that tag equals total *assets* for ~99% of filers, so putting it in the chain would set
        # liabilities = assets for precisely the companies being repaired, forcing TL/TA to 1.0 and
        # tripping Ohlson's insolvency indicator. The repair would have introduced a worse bug than
        # the gap it closed.
        equity_total = df["equity"]
        if "minority_interest" in df.columns:
            equity_total = equity_total + df["minority_interest"].fillna(0.0)
        _fill(df, "liabilities", df["assets"] - equity_total)
    if "total_debt" not in df:
        # Only components the company actually reports somewhere. A filer that never tags
        # short-term borrowings at all would otherwise have every quarter refused for a missing
        # piece it does not have, which trades one wrong number for no number.
        parts = [
            c
            for c in ("long_term_debt", "short_term_debt")
            if c in df.columns and df[c].notna().any()
        ]
        if parts:
            # min_count=len(parts), not 1. Filers tag different components in different quarters:
            # Lowe's most recent quarter carries only short-term borrowings of $380M, so summing
            # "whatever is present" reported that as the company's entire debt against a true
            # $39.8B — a 99% understatement that made a leveraged retailer look debt-free.
            # Requiring every known component leaves the row missing instead, and `latest()` then
            # falls back to the most recent quarter where the full picture was filed.
            df["total_debt"] = df[parts].sum(axis=1, min_count=len(parts))
    if "net_debt" not in df and "total_debt" in df and "cash" in df:
        df["net_debt"] = df["total_debt"] - df["cash"]
    if {"net_income", "income_tax"} <= set(df.columns):
        # Recovers what removing the domestic-only geographic subtotal gave up: that tag reported a
        # third of McDonald's true pretax income, while this puts it at $11.11B against $8.68B of
        # net income — pretax above net income, as it must be for a taxpayer.
        #
        # Only a fallback, because the identity is not universal. It holds exactly for Apple and
        # J&J (relative error 0.0000) but is 41% off for Caterpillar, where non-controlling
        # interests and discontinued operations sit between the two lines. A filed tag therefore
        # always wins; this fills absence only, and minority interest is netted out where tagged.
        derived_pretax = df["net_income"] + df["income_tax"]
        if "minority_interest" in df.columns:
            derived_pretax = derived_pretax + df["minority_interest"].fillna(0.0)
        _fill(df, "pretax_income", derived_pretax)
    if "ebit" not in df and "operating_income" in df:
        df["ebit"] = df["operating_income"]
    elif "ebit" not in df and {"pretax_income", "interest_expense"} <= set(df.columns):
        df["ebit"] = df["pretax_income"] + df["interest_expense"]
    if "ebitda" not in df and {"ebit", "depreciation_amortization"} <= set(df.columns):
        df["ebitda"] = df["ebit"] + df["depreciation_amortization"]
    if "fcf" not in df and {"cfo", "capex"} <= set(df.columns):
        # Capex is filed as a positive outflow — measured 0 negatives in 1,547 raw records across
        # fourteen large filers. So `.abs()` here never corrected a sign convention; it only ever
        # turned a failed Q4 derivation into a plausible number. Target's -$953M derived capex
        # became a +$953M outflow inside free cash flow, off by $1.9B and completely invisible.
        # The Q4 plausibility gate now rejects such values, and without the mask a negative that
        # slips through propagates visibly instead of silently.
        df["fcf"] = df["cfo"] - df["capex"]
    if {"current_assets", "current_liabilities"} <= set(df.columns):
        _fill(df, "working_capital", df["current_assets"] - df["current_liabilities"])
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


class _BulkFactsProvider(Protocol):
    def company_facts(self, cik: str) -> dict[str, Any] | None: ...


class SECProvider:
    """Fundamentals provider backed by SEC EDGAR. Prices, if any, come from elsewhere."""

    name = "sec"
    provides_prices = False
    provides_fundamentals = True

    def __init__(
        self, client: SECClient | None = None, *, bulk: _BulkFactsProvider | None = None
    ) -> None:
        self.client = client or SECClient()
        self.bulk = bulk
        self._tickers: dict[str, str] | None = None

    def resolve_cik(self, ticker: str) -> str | None:
        """Ticker to CIK, tolerating both share-class spellings.

        SEC writes Berkshire's B shares as ``BRK-B``; essentially every other source, and every
        human, writes ``BRK.B``. A bare dict lookup returned None for the dot form and the
        diagnostic then blamed delisting, which is both wrong and misleading.
        """
        if self._tickers is None:
            self._tickers = self.client.ticker_map()
        for candidate in ticker_variants(ticker):
            found = self._tickers.get(candidate)
            if found:
                return found
        return None

    def fetch_by_cik(
        self,
        cik: str,
        *,
        ticker: str | None = None,
        asof: date | None = None,
        pit_mode: PitMode = PitMode.LATEST,
        refresh: bool = False,
    ) -> SecuritySnapshot:
        """Fetch by CIK, bypassing the ticker map entirely.

        The supported entry point for historical work. ``company_tickers.json`` lists only
        **currently-listed** issuers, so a ticker-keyed historical run is survivorship-filtered by
        construction — and worse, tickers get reused: ``BBBY`` resolves today to CIK 1130713, the
        entity that bought the brand out of bankruptcy, while the retailer that actually failed is
        CIK 886158, now named "20230930-DK-Butterfly-1, Inc." with no ticker at all. Grading the
        survivor in place of the casualty is silent and total.

        CIKs are permanent, so keying fixtures and historical universes on them is the only way to
        study companies that no longer exist.
        """
        return self._fetch(
            str(cik).zfill(10),
            ticker or str(cik),
            asof=asof or date.today(),
            pit_mode=pit_mode,
            refresh=refresh,
        )

    def fetch(
        self,
        ticker: str,
        *,
        asof: date | None = None,
        pit_mode: PitMode = PitMode.LATEST,
        refresh: bool = False,
    ) -> SecuritySnapshot:
        """Build a snapshot with fundamentals, sector and share count. Never raises on data gaps."""
        if asof is not None and asof != date.today() and pit_mode is not PitMode.PIT:
            # `_select` filters on filed <= asof only under PIT, so a historical asof would be
            # accepted and then ignored. Measured on Bed Bath & Beyond at asof=2019-01-01: PIT
            # returns assets of $7.32B from a 2018-09-01 balance sheet, the default returns $2.23B
            # from 2023-02-25 — four years of leakage from one omitted keyword, which would let a
            # distress model score companies on their post-bankruptcy balance sheets.
            raise ValueError(
                f"asof={asof} is ignored under PitMode.LATEST. Pass pit_mode=PitMode.PIT for "
                f"point-in-time selection, or drop asof to grade as of today."
            )
        asof = asof or date.today()
        cik = self.resolve_cik(ticker)
        if cik is None:
            snap = SecuritySnapshot(ticker=ticker.upper(), asof=asof)
            snap.warnings.append(
                f"{ticker}: not in SEC's ticker map, which lists only currently-listed issuers. "
                f"Delisted and acquired companies are absent (SIVB, FRC, WE and BBBY's failed "
                f"entity all resolve to nothing); use fetch_by_cik for those."
            )
            return snap
        return self._fetch(cik, ticker, asof=asof, pit_mode=pit_mode, refresh=refresh)

    def _fetch(
        self,
        cik: str,
        ticker: str,
        *,
        asof: date,
        pit_mode: PitMode = PitMode.LATEST,
        refresh: bool = False,
    ) -> SecuritySnapshot:
        """Shared body for :meth:`fetch` and :meth:`fetch_by_cik`."""
        if asof != date.today() and pit_mode is not PitMode.PIT:
            # Keep this invariant at the lowest shared entry point. Historically fetch() enforced
            # it while fetch_by_cik() bypassed it, so the CIK path recommended for delisted issuers
            # silently admitted post-as-of restatements.
            raise ValueError(
                f"asof={asof} is ignored under PitMode.LATEST. Pass pit_mode=PitMode.PIT for "
                "point-in-time selection, or use today's date for a latest-vintage snapshot."
            )
        snap = SecuritySnapshot(ticker=ticker.upper(), asof=asof)
        snap.cik = cik

        subs = self.client.submissions(cik, refresh=refresh)
        if subs:
            snap.name = subs.get("name")
            snap.sic = subs.get("sic")
            snap.industry = subs.get("sicDescription")
            snap.sector = classify_sic(subs.get("sic"))
            snap.meta["sic_source"] = "current_sec_submissions_metadata"
            if asof != date.today():
                snap.warnings.append(
                    "historical fundamentals use the issuer's current SEC submissions SIC; "
                    "SEC Companyfacts does not provide a dated SIC history, so historical metric "
                    "applicability may reflect a later business classification"
                )

        facts = self.bulk.company_facts(cik) if self.bulk is not None else None
        if facts is None:
            facts = self.client.company_facts(cik, refresh=refresh)
        if not facts:
            snap.warnings.append(f"{ticker}: SEC companyfacts unavailable")
            return snap

        snap.fundamentals = build_fundamentals(
            facts, pit_mode=pit_mode, asof=asof, sector=snap.sector.value
        )
        currencies = detect_currencies(facts)
        foreign = currencies - {"USD"}
        if foreign:
            snap.meta["currencies"] = sorted(currencies)
            if "USD" not in currencies:
                snap.warnings.append(
                    f"{ticker} reports in {'/'.join(sorted(foreign))}, not USD — those facts are "
                    f"dropped rather than mixed with dollar figures, so coverage will be low"
                )
            else:
                snap.warnings.append(
                    f"{ticker} files some facts in {'/'.join(sorted(foreign))}; those are excluded "
                    f"to avoid mixing currencies, so a few metrics may be missing"
                )
        snap.meta["pit_mode"] = pit_mode.value
        snap.meta["tags_used"] = snap.fundamentals.tag_used

        dei = facts.get("facts", {}).get("dei", {})
        for concept, chain in DEI_CONCEPTS.items():
            tag = next((t for t in chain if t in dei), None)
            if tag is None:
                continue
            series = normalize_instant_facts(dei[tag], pit_mode=pit_mode, asof=asof)
            # normalize_instant_facts keys on the fact's own end date and filters by `filed` only
            # under PIT, so without this a --asof 2020 run computed market_cap = price(2020) x
            # shares(2026): silent look-ahead in the denominator of every valuation metric.
            if asof is not None:
                series = series[[d for d in series.index if d <= asof]]
            if series.empty:
                snap.warnings.append(f"no {concept} observation on or before {asof}")
                continue
            # The cover-page count needs the same age bound as every other figure. Mastercard's
            # newest DEI observation is dated 2010-10-27 at 122.5M shares against a real ~900M, and
            # Visa's is 2010-01-27 — so market cap came out 7.3x too small and every valuation
            # multiple 7.3x too cheap, at full reported coverage.
            if asof is not None and (asof - series.index[-1]).days > _MAX_FACT_AGE_DAYS:
                snap.warnings.append(
                    f"{concept}: newest cover-page value is dated {series.index[-1]}, too stale "
                    f"to use"
                )
                continue
            if concept == "shares_outstanding":
                snap.shares_outstanding = float(series.iloc[-1])
                snap.meta["shares_date"] = series.index[-1]
                snap.meta["shares_history"] = series
            elif concept == "public_float":
                snap.public_float = float(series.iloc[-1])
                snap.meta["public_float_date"] = series.index[-1]
                # The full dated history, not just the latest value: pricing from public float
                # needs a float and a market price measured on the *same* date to solve the
                # affiliate share, and only the history offers a choice of dates to match against.
                snap.meta["public_float_history"] = series

        # Diluted share count from the income statement is the better denominator for per-share
        # work when it is available; the dei cover-page count is a fallback.
        if snap.fundamentals is not None:
            diluted = snap.fundamentals.latest("shares_diluted")
            if diluted:
                # Filers tag share counts in whatever unit their statements are presented in.
                # McDonald's reports diluted shares as **716** against a DEI cover-page count of
                # 710,505,859 — a factor of a million. Nothing else in the payload says so, and a
                # market cap computed from it would come out at $180 thousand rather than $180
                # billion, making the company look absurdly cheap on every multiple.
                #
                # The DEI cover count is always in whole shares, so it is the scale oracle. Only
                # clean powers of a thousand are corrected: anything else is a genuine difference
                # between weighted-average diluted and period-end basic shares, not a unit error.
                if snap.shares_outstanding:
                    ratio = snap.shares_outstanding / diluted
                    for factor in (1_000_000.0, 1_000.0):
                        if 0.5 * factor <= ratio <= 2.0 * factor:
                            diluted *= factor
                            snap.meta["shares_scale_corrected"] = factor
                            snap.warnings.append(
                                f"{ticker}: diluted share count was tagged in units of {factor:,.0f}; "
                                f"rescaled against the DEI cover-page count"
                            )
                            break
                snap.meta["shares_diluted"] = diluted
                if snap.shares_outstanding is None:
                    snap.shares_outstanding = diluted
                    snap.meta["shares_source"] = "income_statement_diluted"
                    snap.warnings.append(
                        f"{ticker}: no DEI cover-page share count, so the scale of the diluted "
                        f"count could not be cross-checked"
                    )

        if snap.shares_outstanding is None and snap.fundamentals is not None:
            # Last resort: shares = net income / diluted EPS, an identity. Visa tags its
            # multi-class shares dimensionally, so companyfacts exposes no consolidated diluted
            # count at all, and its only DEI cover-page records are from 2009 and 2010 — leaving
            # a $500B company with no share count and therefore no valuation metrics whatsoever.
            income = snap.fundamentals.ttm("net_income")
            eps = snap.fundamentals.ttm("eps_diluted")
            if income is not None and eps and abs(eps) > 1e-9:
                implied = income / eps
                if implied > 0:
                    snap.shares_outstanding = implied
                    snap.meta["shares_source"] = "implied_from_eps"
                    snap.warnings.append(
                        f"{ticker}: share count implied from net income divided by diluted EPS; "
                        f"no usable cover-page or income-statement count was available"
                    )

        return snap
