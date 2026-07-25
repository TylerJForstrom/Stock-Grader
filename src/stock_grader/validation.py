"""Validating the grader against real outcomes — without needing any price data.

The usual way to test a stock grader is to check whether good grades earn better forward returns.
That needs a price history, which is exactly what is unavailable here. But returns are not the only
observable outcome, and for the solvency and earnings-quality pillars they are not even the most
direct one.

EDGAR full-text search exposes several **real, free, labelled outcomes**, measured 2026-07-24:

===================================  =====  =============
label                                form   2023 filings
===================================  =====  =============
going-concern opinion                10-K   853
Chapter 11 bankruptcy                8-K    1,847
restatement (Item 4.02 non-reliance) 8-K    296
material weakness in controls        10-K   401
===================================  =====  =============

A going-concern opinion is an *auditor* stating substantial doubt the company survives twelve
months. If the health pillar cannot separate those companies from ordinary ones, it does not work —
and that is checkable today, with no prices, no forward returns and no survivorship-free universe.

Everything here grades **point-in-time as of the filing date**, so the grader sees only what was
public then. Grading a company today on the strength of its 2023 filing would leak the outcome
straight into the input.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

__all__ = ["OutcomeLabel", "LABEL_QUERIES", "EdgarLabelSearch", "separation_report", "auc"]

_EFTS = "https://efts.sec.gov/LATEST/search-index"

# Query phrasing matters: these are the standard formulations auditors and filers use.
LABEL_QUERIES: dict[str, tuple[str, str]] = {
    "going_concern": (
        '"substantial doubt about its ability to continue as a going concern"',
        "10-K",
    ),
    "bankruptcy": ('"chapter 11"', "8-K"),
    "restatement": ('"non-reliance on previously issued financial statements"', "8-K"),
    "material_weakness": ('"material weakness in internal control"', "10-K"),
}


@dataclass(slots=True)
class OutcomeLabel:
    """One company with a known, dated outcome."""

    cik: str
    name: str
    filed: date
    label: str
    ticker: str | None = None
    meta: dict = field(default_factory=dict)


class EdgarLabelSearch:
    """Query EDGAR full-text search for companies with a known outcome.

    Full-text search covers filings from 2001 onward. Results are capped at 10,000 per query by the
    API, and paging is 10 hits at a time, so a large sample takes many requests — the rate limiter
    keeps that within SEC's guidance.
    """

    def __init__(self, *, contact: str | None = None, rate: float = 5.0, timeout: float = 30.0) -> None:
        import os

        self.contact = contact or os.environ.get("STOCK_GRADER_CONTACT") or "stock-grader"
        self.timeout = timeout
        self._min_interval = 1.0 / rate
        self._last = 0.0

    def _get(self, params: dict) -> dict | None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()
        try:
            response = requests.get(
                _EFTS, params=params,
                headers={"User-Agent": f"Stock-Grader/0.1 ({self.contact})"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            log.warning("EDGAR full-text search failed: %s", exc)
            return None
        if response.status_code != 200:
            log.warning("EDGAR full-text search returned HTTP %s", response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def search(
        self,
        label: str,
        *,
        start: date,
        end: date,
        limit: int = 100,
    ) -> list[OutcomeLabel]:
        """Companies whose filings in the window carry ``label``, deduplicated by CIK."""
        if label not in LABEL_QUERIES:
            raise KeyError(f"unknown label {label!r}; available: {', '.join(LABEL_QUERIES)}")
        query, forms = LABEL_QUERIES[label]

        found: dict[str, OutcomeLabel] = {}
        offset = 0
        while len(found) < limit and offset < 1000:
            payload = self._get({
                "q": query, "forms": forms, "dateRange": "custom",
                "startdt": start.isoformat(), "enddt": end.isoformat(), "from": offset,
            })
            hits = ((payload or {}).get("hits") or {}).get("hits") or []
            if not hits:
                break
            for hit in hits:
                source = hit.get("_source", {})
                ciks = source.get("ciks") or []
                if not ciks:
                    continue
                cik = str(ciks[0]).zfill(10)
                if cik in found:
                    continue
                display = (source.get("display_names") or ["unknown"])[0]
                filed = source.get("file_date")
                try:
                    filed_date = date.fromisoformat(filed) if filed else end
                except ValueError:
                    filed_date = end
                found[cik] = OutcomeLabel(
                    cik=cik, name=display, filed=filed_date, label=label,
                    ticker=_ticker_from_display(display),
                )
            offset += 10
        return list(found.values())[:limit]


def _ticker_from_display(display: str) -> str | None:
    """EDGAR formats names as ``Company Name  (TICK, TICKW)  (CIK 000...)``."""
    if "(" not in display:
        return None
    for chunk in display.split("(")[1:]:
        candidate = chunk.split(")")[0].strip()
        if candidate.upper().startswith("CIK"):
            continue
        first = candidate.split(",")[0].strip().upper()
        if first and 1 <= len(first) <= 5 and first.isalpha():
            return first
    return None


def auc(positive: list[float], negative: list[float]) -> float | None:
    """Area under the ROC curve, via the Mann-Whitney U statistic.

    Reads as: the probability that a randomly chosen distressed company scores worse than a randomly
    chosen healthy one. 0.5 is a coin flip, 1.0 is perfect separation. Computed from ranks, so it
    needs no threshold and is unaffected by the scores' scale.
    """
    clean_pos = [float(v) for v in positive if v is not None and np.isfinite(v)]
    clean_neg = [float(v) for v in negative if v is not None and np.isfinite(v)]
    if not clean_pos or not clean_neg:
        return None
    combined = pd.Series(clean_pos + clean_neg)
    ranks = combined.rank()
    n_pos, n_neg = len(clean_pos), len(clean_neg)
    rank_sum = float(ranks.iloc[:n_pos].sum())
    u = rank_sum - n_pos * (n_pos + 1) / 2.0
    return float(1.0 - u / (n_pos * n_neg))  # 1 - : lower score should mean distressed


def separation_report(
    distressed: dict[str, float],
    healthy: dict[str, float],
    *,
    metric_name: str = "score",
) -> dict:
    """Compare a metric's distribution between a labelled and a control group.

    Reports AUC alongside the medians and sample sizes. AUC near 0.5 means the metric carries no
    information about the outcome — which is a finding, not a failure of the experiment.
    """
    pos = [v for v in distressed.values() if v is not None and np.isfinite(v)]
    neg = [v for v in healthy.values() if v is not None and np.isfinite(v)]
    return {
        "metric": metric_name,
        "n_distressed": len(pos),
        "n_healthy": len(neg),
        "median_distressed": float(np.median(pos)) if pos else None,
        "median_healthy": float(np.median(neg)) if neg else None,
        "auc": auc(pos, neg),
        "coverage_distressed": len(pos) / max(len(distressed), 1),
        "coverage_healthy": len(neg) / max(len(healthy), 1),
    }
