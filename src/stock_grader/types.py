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
    "PeriodType",
    "PitMode",
    "SectorClass",
    "Fundamentals",
    "SecuritySnapshot",
    "MetricResult",
    "PillarScore",
    "GradeReport",
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
                # Capex is filed as a positive outflow, so free cash flow subtracts its magnitude.
                total += sign * (abs(part) if component == "capex" else part)
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

    def latest(self, concept: str, *, annual: bool = False) -> float | None:
        """Most recent reported value for a concept."""
        frame = self.annual if annual else self.quarterly
        if concept not in frame.columns:
            return None
        series = frame[concept].dropna()
        return float(series.iloc[-1]) if not series.empty else None

    def history(self, concept: str, n: int, *, annual: bool = True) -> pd.Series | None:
        """Last ``n`` periods of a concept, oldest first, or ``None`` if insufficient."""
        frame = self.annual if annual else self.quarterly
        if concept not in frame.columns:
            return None
        series = frame[concept].dropna()
        return series.iloc[-n:] if len(series) >= n else None


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
