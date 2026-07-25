"""Core data types shared by every layer of the grader.

The pipeline is::

    DataProvider -> SecuritySnapshot -> MetricResult[] -> PillarScore[] -> GradeReport

Every type here is deliberately dumb: no I/O, no computation beyond trivial derivations, so that
the metric, weighting and grading layers can be unit-tested against hand-built objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, ClassVar

import pandas as pd

__all__ = [
    "Coverage",
    "Fundamentals",
    "GradeReport",
    "MetricResult",
    "PeriodType",
    "PillarScore",
    "PitMode",
    "SectorClass",
    "SecuritySnapshot",
]


class Coverage(str, Enum):
    """Why a metric does or does not have a value.

    The distinction between ``MISSING`` and ``NOT_APPLICABLE`` is load-bearing: a bank has no
    current ratio because banks do not publish a classified balance sheet, not because the data
    failed to download. Charging a coverage penalty for the former would unfairly widen the
    confidence interval of every financial in the universe. See docs/design/DATA-GROUND-TRUTH.md §6.
    """

    OK = "ok"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"

    @property
    def penalised(self) -> bool:
        """Whether this state counts against the security's data-coverage score."""
        return self is Coverage.MISSING


class PeriodType(str, Enum):
    """Shape of a reported fact."""

    INSTANT = "instant"  # balance-sheet: a stock at a point in time
    DURATION = "duration"  # income/cash-flow: a flow over a window


class PitMode(str, Enum):
    """Which vintage of a restated figure to use.

    ``PIT`` answers "what could an investor have known on this date"; ``LATEST`` answers "what is
    the most accurate figure available now". Mixing them within one grade is a correctness bug, so
    the mode is fixed once per run and recorded in :attr:`GradeReport.meta`.
    """

    PIT = "pit"
    LATEST = "latest"


class SectorClass(str, Enum):
    """Business-model class, derived from SIC. Drives the metric applicability matrix."""

    BANK = "bank"
    INSURANCE = "insurance"
    REIT = "reit"
    HOLDING = "holding"
    UTILITY = "utility"
    ENERGY = "energy"
    GENERAL = "general"

    @property
    def has_classified_balance_sheet(self) -> bool:
        """False where current/non-current is not a meaningful split."""
        return self not in (SectorClass.BANK, SectorClass.INSURANCE, SectorClass.REIT, SectorClass.HOLDING)

    @property
    def has_cogs(self) -> bool:
        """False where 'cost of goods sold' and therefore gross margin are undefined."""
        return self not in (SectorClass.BANK, SectorClass.INSURANCE, SectorClass.REIT, SectorClass.HOLDING)


@dataclass(slots=True)
class Fundamentals:
    """Normalised financial statements for one security.

    ``quarterly`` and ``annual`` are DataFrames indexed by period end date, columns are canonical
    concept names (``revenue``, ``net_income``, ...) — never raw XBRL tags. ``filed`` carries the
    publication date of each row so downstream code can enforce point-in-time correctness without
    re-deriving it.
    """

    quarterly: pd.DataFrame
    annual: pd.DataFrame
    filed: pd.Series
    period_type: dict[str, PeriodType] = field(default_factory=dict)
    tag_used: dict[str, str] = field(default_factory=dict)
    pit_mode: PitMode = PitMode.LATEST
    currency: str = "USD"
    averaged: set[str] = field(default_factory=set)

    # Concepts that are arithmetic combinations of others. Summing a per-period derived column is
    # not equivalent to combining the components' own trailing sums, because each component has its
    # own missingness pattern: a quarter where D&A was reported but EBIT was not drops out of a
    # per-row EBITDA entirely. Simon Property Group's EBITDA came out *below* its EBIT that way.
    # Composing at the trailing-twelve-month level uses each component's best available window.
    _COMPOSITES: ClassVar[dict[str, tuple[tuple[str, int], ...]]] = {
        "ebitda": (("ebit", 1), ("depreciation_amortization", 1)),
        "fcf": (("cfo", 1), ("capex", -1)),
    }

    def ttm(self, concept: str) -> float | None:
        """Trailing-twelve-month value: summed for flows, averaged for averages, latest for stocks.

        Returns ``None`` rather than a partial sum when fewer than four quarters are available —
        a three-quarter "TTM" understates a flow by roughly a quarter and is worse than no answer.

        Concepts in :attr:`averaged` (weighted-average share counts) are averaged rather than
        summed. Summing four quarterly average share counts would report a company as having four
        times the shares it does, and quadrupling the denominator of every per-share figure.
        """
        if concept in self._COMPOSITES:
            total = 0.0
            for component, sign in self._COMPOSITES[concept]:
                part = self.ttm(component)
                if part is None:
                    return None
                # No abs(): capex is filed as a positive outflow (0 negatives in 1,547 sampled
                # records), so a negative here is a derivation failure that must stay visible.
                total += sign * part
            return float(total)
        if concept not in self.quarterly.columns:
            return None
        series = self.quarterly[concept].dropna()
        if series.empty:
            return None
        if self.period_type.get(concept, PeriodType.DURATION) is PeriodType.INSTANT:
            return float(series.iloc[-1])
        if len(series) < 4:
            return None
        window = series.iloc[-4:]
        if not self._is_contiguous_year(window):
            return None
        if concept in self.averaged:
            return float(window.mean())
        return float(window.sum())

    @staticmethod
    def _is_contiguous_year(window: pd.Series) -> bool:
        """Whether four quarterly observations actually form one consecutive year.

        ``dropna`` collapses gaps, so the last four *available* values may be scattered across
        different years — which is exactly what happened to a derived EBITDA column whose inputs
        had different missingness patterns, producing an EBITDA below its own EBIT. Requiring the
        window to span roughly twelve months turns that silent corruption into an honest ``None``.
        """
        try:
            span = (pd.Timestamp(window.index[-1]) - pd.Timestamp(window.index[0])).days
        except (TypeError, ValueError):
            return True  # non-date index: nothing to check
        return 240 <= span <= 400

    # Balance-sheet concepts assembled from components. Resolved from each component's own most
    # recent observation rather than row-wise, because filers tag the pieces in different quarters:
    # Home Depot and Target never file long-term and short-term debt in the same period, so a
    # row-wise sum is permanently empty for them while each component is individually available.
    _INSTANT_COMPOSITES: ClassVar[dict[str, tuple[str, ...]]] = {
        "total_debt": ("long_term_debt", "short_term_debt"),
    }

    def latest(
        self,
        concept: str,
        *,
        annual: bool = False,
        asof: date | None = None,
        max_age_days: int | None = None,
    ) -> float | None:
        """Most recent reported value for a concept, optionally refusing stale ones.

        Without an age bound this returns the last non-NaN value however old it is, which is how a
        balance-sheet figure abandoned years ago reaches a ratio as though it were current.

        ``max_age_days`` defaults to unbounded so existing callers are unaffected; the metric layer
        passes ~400 days. That bound is deliberately generous rather than tight: a 10-K-only filer's
        balance sheet is legitimately up to fifteen months old, and a stricter limit would delete
        those companies rather than protect them.
        """
        frame = self.annual if annual else self.quarterly
        direct = self._latest_direct(frame, concept, asof, max_age_days)
        if direct is not None:
            return direct
        # The stored column was absent, empty, or too old. Rebuilding from components can still
        # succeed, because filers tag the pieces in different quarters than the whole: Home Depot's
        # combined debt row was last filed in January 2024 while its long-term debt is current.
        components = self._INSTANT_COMPOSITES.get(concept)
        if not components:
            return None
        reported = [c for c in components if c in frame.columns and frame[c].notna().any()]
        parts = [
            self._latest_direct(frame, c, asof, max_age_days) for c in reported
        ]
        # Every component the company reports must resolve. A partial sum here is exactly what
        # reported Lowe's $39.8B of debt as $380M, and the error runs one way: it understates
        # leverage, making a company look safer and cheaper than it is.
        if parts and all(p is not None for p in parts):
            return float(sum(parts))  # type: ignore[arg-type]
        return None

    @staticmethod
    def _latest_direct(
        frame: pd.DataFrame,
        concept: str,
        asof: date | None,
        max_age_days: int | None,
    ) -> float | None:
        """Last value of a stored column, subject to the age bound."""
        if concept not in frame.columns:
            return None
        series = frame[concept].dropna()
        if series.empty:
            return None
        if asof is not None and max_age_days is not None:
            try:
                age = (pd.Timestamp(asof) - pd.Timestamp(series.index[-1])).days
            except (TypeError, ValueError):
                age = 0
            if age > max_age_days:
                return None
        return float(series.iloc[-1])

    def history(
        self,
        concept: str,
        n: int,
        *,
        annual: bool = True,
        require_span: bool = True,
    ) -> pd.Series | None:
        """Last ``n`` periods of a concept, oldest first, or ``None`` if insufficient.

        When ``annual`` and ``require_span``, the window must actually cover roughly ``n - 1``
        years. ``dropna`` collapses gaps, so "the last 6 annual rows" can silently be six quarters
        (a 1.25-year span reported as a 5-year CAGR) or six rows spanning 2010 to 2025 (a 15-year
        span reported the same way). Both were happening. Returning ``None`` costs some growth-metric
        coverage and is worth it: a growth rate over the wrong window is not a conservative
        estimate, it is a wrong number wearing the right label.
        """
        frame = self.annual if annual else self.quarterly
        if concept not in frame.columns:
            return None
        series = frame[concept].dropna()
        if len(series) < n:
            return None
        window = series.iloc[-n:]
        if annual and require_span and n > 1:
            try:
                elapsed = (pd.Timestamp(window.index[-1]) - pd.Timestamp(window.index[0])).days / 365.25
            except (TypeError, ValueError):
                return window
            expected = n - 1
            if not (expected * 0.75) <= elapsed <= (expected * 1.35):
                return None
        return window


@dataclass(slots=True)
class SecuritySnapshot:
    """Everything known about one security at one moment. The sole input to the metric engine."""

    ticker: str
    asof: date
    fundamentals: Fundamentals | None = None
    prices: pd.DataFrame | None = None  # index=date, cols: open/high/low/close/adj_close/volume
    benchmark: pd.DataFrame | None = None
    risk_free: pd.Series | None = None
    cik: str | None = None
    name: str | None = None
    sic: str | None = None
    sector: SectorClass = SectorClass.GENERAL
    industry: str | None = None
    shares_outstanding: float | None = None
    public_float: float | None = None
    price: float | None = None
    currency: str = "USD"
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_prices(self) -> bool:
        return self.prices is not None and not self.prices.empty

    @property
    def market_cap(self) -> float | None:
        if self.price is None or self.shares_outstanding is None:
            return None
        return self.price * self.shares_outstanding

    @property
    def synthetic_prices(self) -> bool:
        """True when the price series is generated, not real market history."""
        return bool(self.meta.get("synthetic_prices", False))


@dataclass(slots=True)
class MetricResult:
    """One computed metric, with enough provenance to explain and audit it."""

    name: str
    pillar: str
    value: float | None
    direction: int  # +1 higher-is-better, -1 lower-is-better, 0 non-monotonic (ideal band)
    coverage: Coverage = Coverage.OK
    raw_inputs: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    unit: str = ""

    @property
    def usable(self) -> bool:
        return self.coverage is Coverage.OK and self.value is not None


@dataclass(slots=True)
class PillarScore:
    """Aggregate of the metrics belonging to one pillar."""

    pillar: str
    score: float  # 0..100
    weights: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    metric_scores: dict[str, float] = field(default_factory=dict)
    coverage: float = 1.0  # fraction of applicable metrics actually computed
    n_metrics: int = 0
    n_missing: int = 0
    n_not_applicable: int = 0
    weighting_method: str = ""
    aggregator: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GradeReport:
    """The final artefact: a grade, why it is that grade, and how much to trust it."""

    ticker: str
    asof: date
    profile: str
    score: float  # 0..100
    letter: str
    pillars: dict[str, PillarScore] = field(default_factory=dict)
    pillar_weights: dict[str, float] = field(default_factory=dict)
    effective_pillar_weights: dict[str, float] = field(default_factory=dict)
    lost_weight: float = 0.0
    percentile: float | None = None
    ci: tuple[float, float] | None = None
    coverage: float = 1.0
    weighting_method: str = ""
    normalizer: str = ""
    aggregator: str = ""
    gates: list[str] = field(default_factory=list)  # hard gates that capped the grade
    explain: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def graded(self) -> bool:
        """False when coverage was too low to responsibly issue a grade."""
        return self.letter != "N/A"

    def top_contributors(self, n: int = 5, *, positive: bool = True) -> list[tuple[str, float]]:
        """Metrics that moved the grade most, signed relative to a neutral 50 score."""
        flat: list[tuple[str, float]] = []
        for pillar, ps in self.pillars.items():
            pw = self.pillar_weights.get(pillar, 0.0)
            for metric, contribution in ps.contributions.items():
                flat.append((metric, contribution * pw))
        flat.sort(key=lambda kv: kv[1], reverse=positive)
        return flat[:n]
