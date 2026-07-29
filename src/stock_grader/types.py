"""Core data types shared by every layer of the grader.

The pipeline is::

    DataProvider -> SecuritySnapshot -> MetricResult[] -> PillarScore[] -> GradeReport

Every type here is deliberately dumb: no I/O, no computation beyond trivial derivations, so that
the metric, weighting and grading layers can be unit-tested against hand-built objects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, ClassVar

import numpy as np
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
    model-sensitivity range of every financial in the universe. See
    docs/design/DATA-GROUND-TRUTH.md §6.
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
    # Per-concept audit trail: {"revenue": {"tag": ..., "unit": ...,
    # "latest_period_end": ..., "latest_filed": ...}} — the evidence link from
    # every ratio back to the specific XBRL disclosure it was computed from.
    concept_provenance: dict[str, dict] = field(default_factory=dict)
    pit_mode: PitMode = PitMode.LATEST
    currency: str = "USD"
    averaged: set[str] = field(default_factory=set)

    # Concepts that are arithmetic combinations of others. Each composite must use one exact
    # four-quarter window shared by every component. Independently selected trailing windows can
    # describe different fiscal periods, while a per-row derived column can silently drop quarters
    # with asymmetric missingness; both produce a number with no coherent reporting basis.
    _COMPOSITES: ClassVar[dict[str, tuple[tuple[str, int], ...]]] = {
        "ebitda": (("ebit", 1), ("depreciation_amortization", 1)),
        "fcf": (("cfo", 1), ("capex", -1)),
    }

    def ttm(
        self,
        concept: str,
        *,
        asof: date | None = None,
        max_age_days: int | None = None,
    ) -> float | None:
        """Trailing-twelve-month value: summed for flows, averaged for averages, latest for stocks.

        Returns ``None`` rather than a partial sum when fewer than four quarters are available —
        a three-quarter "TTM" understates a flow by roughly a quarter and is worse than no answer.

        Concepts in :attr:`averaged` (weighted-average share counts) are averaged rather than
        summed. Summing four quarterly average share counts would report a company as having four
        times the shares it does, and quadrupling the denominator of every per-share figure.

        **The age bound is not optional in practice.** ``_is_contiguous_year`` only checks that the
        four quarters span a year *internally*; it never asks whether that year is anywhere near
        ``asof``. Without a bound, Mastercard's net income resolved to a window ending 2014-03-31 —
        twelve years stale — and was divided by 2026 revenue to report a 9.5% net margin against a
        true 45%. AbbVie combined 2018 revenue with 2026 EBIT inside one ratio. Boeing reported a
        2.2% buyback yield from a 2019 window, having repurchased nothing since. All at
        ``Coverage.OK``, with no warning. Measured across the default universe: **153 stale values
        spanning 60 of 82 companies**.

        Defaults to unbounded so existing callers are unaffected; the metric layer passes ~400 days.
        """
        if concept in self._COMPOSITES:
            components = [component for component, _ in self._COMPOSITES[concept]]
            aligned = self.aligned_quarterly(
                components,
                periods=4,
                asof=asof,
                max_age_days=max_age_days,
            )
            if aligned is None:
                return None
            total = 0.0
            for component, sign in self._COMPOSITES[concept]:
                part = (
                    float(aligned[component].mean())
                    if component in self.averaged
                    else float(aligned[component].sum())
                )
                # No abs(): capex is filed as a positive outflow (0 negatives in 1,547 sampled
                # records), so a negative here is a derivation failure that must stay visible.
                total += sign * part
            return float(total)
        series = self._dated_series(self.quarterly, concept, asof=asof)
        if series is None:
            return None
        if self._too_old(series.index[-1], asof, max_age_days):
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
    def _too_old(observed, asof: date | None, max_age_days: int | None) -> bool:
        """Whether an observation is too far before ``asof`` to describe the company now."""
        if asof is None or max_age_days is None:
            return False
        try:
            return (pd.Timestamp(asof) - pd.Timestamp(observed)).days > max_age_days
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_contiguous_year(window: pd.Series) -> bool:
        """Whether four quarterly observations actually form one consecutive year.

        ``dropna`` collapses gaps, so the last four *available* values may be scattered across
        different years — which is exactly what happened to a derived EBITDA column whose inputs
        had different missingness patterns, producing an EBITDA below its own EBIT. Requiring the
        window to span roughly twelve months, with plausible quarter-to-quarter gaps, turns that
        silent corruption into an honest ``None``.
        """
        try:
            index = pd.DatetimeIndex(pd.to_datetime(window.index)).sort_values()
            span = int((index[-1] - index[0]).days)
            gaps = index.to_series().diff().dt.days.dropna()
        except (TypeError, ValueError):
            return True  # non-date index: nothing to check
        return 240 <= span <= 400 and bool(gaps.between(55, 135).all())

    def _clean_dated_frame(
        self,
        frame: pd.DataFrame,
        concepts: list[str],
        *,
        asof: date | None,
        require_complete: bool = True,
    ) -> pd.DataFrame | None:
        """Numeric rows on normalized dates, optionally bounded by publication and analysis date."""
        if not concepts or any(concept not in frame.columns for concept in concepts):
            return None
        selected = frame[concepts].copy()
        parsed = pd.to_datetime(selected.index, errors="coerce", utc=True)
        valid = np.asarray(parsed.notna(), dtype=bool)
        selected = selected.loc[valid]
        selected.index = parsed[valid].tz_convert(None).normalize()
        selected = selected[~selected.index.duplicated(keep="last")].sort_index()
        if asof is not None:
            selected = selected[selected.index <= pd.Timestamp(asof)]
            # Production PIT ingestion already removes records filed after ``asof``. Repeating the
            # row-level check here protects hand-built/imported PIT frames. Missing filing dates
            # remain admissible because instant concepts do not currently populate ``filed``;
            # per-concept filing provenance is added separately by the evidence-schema work.
            if self.pit_mode is PitMode.PIT and not self.filed.empty:
                filed = self.filed.copy()
                filed_index = pd.to_datetime(filed.index, errors="coerce", utc=True)
                valid_filed_index = np.asarray(filed_index.notna(), dtype=bool)
                filed = filed.loc[valid_filed_index]
                filed.index = filed_index[valid_filed_index].tz_convert(None).normalize()
                filed = filed[~filed.index.duplicated(keep="last")].sort_index()
                filed_dates = pd.to_datetime(filed, errors="coerce", utc=True)
                filed_dates = filed_dates.dt.tz_convert(None).dt.normalize()
                aligned_filed = filed_dates.reindex(selected.index)
                selected = selected[aligned_filed.isna() | (aligned_filed <= pd.Timestamp(asof))]
        selected = selected.apply(pd.to_numeric, errors="coerce")
        selected = selected.replace([np.inf, -np.inf], np.nan)
        selected = selected.dropna(how="any" if require_complete else "all")
        return selected if not selected.empty else None

    def _dated_series(
        self,
        frame: pd.DataFrame,
        concept: str,
        *,
        asof: date | None,
    ) -> pd.Series | None:
        """One finite concept series with the same date and PIT filtering used by aligners."""
        selected = self._clean_dated_frame(frame, [concept], asof=asof)
        if selected is None:
            return None
        return selected[concept]

    def aligned_quarterly(
        self,
        concepts: Iterable[str],
        *,
        periods: int = 4,
        asof: date | None = None,
        max_age_days: int | None = None,
    ) -> pd.DataFrame | None:
        """Exact shared consecutive quarterly rows for multi-concept flow calculations."""
        if periods < 1:
            raise ValueError("periods must be positive")
        names = list(dict.fromkeys(concepts))
        frame = self._clean_dated_frame(self.quarterly, names, asof=asof)
        if frame is None or len(frame) < periods:
            return None
        window = frame.iloc[-periods:]
        if self._too_old(window.index[-1], asof, max_age_days):
            return None
        if periods > 1:
            gaps = window.index.to_series().diff().dt.days.dropna()
            if not bool(gaps.between(55, 135).all()):
                return None
            if periods == 4:
                span = int((window.index[-1] - window.index[0]).days)
                if not 240 <= span <= 400:
                    return None
        return window

    def aligned_annual(
        self,
        concepts: Iterable[str],
        *,
        periods: int = 2,
        asof: date | None = None,
        max_age_days: int | None = None,
        required_periods: Mapping[str, int] | None = None,
    ) -> pd.DataFrame | None:
        """Consecutive fiscal rows satisfying each concept's actual lookback requirement.

        ``required_periods`` avoids inventing requirements for a model's unused cells. For
        example, Ohlson needs two years of net income but only the current year's balance-sheet
        and cash-flow inputs. Omitted concepts default to all ``periods``.
        """
        if periods < 1:
            raise ValueError("periods must be positive")
        names = list(dict.fromkeys(concepts))
        requirements = {name: periods for name in names}
        if required_periods is not None:
            unknown = set(required_periods) - set(names)
            if unknown:
                raise ValueError(f"requirements reference unknown concepts: {sorted(unknown)}")
            requirements.update(required_periods)
        if any(count < 1 or count > periods for count in requirements.values()):
            raise ValueError("required period counts must be between 1 and periods")

        frame = self._clean_dated_frame(
            self.annual,
            names,
            asof=asof,
            require_complete=False,
        )
        if frame is None or len(frame) < periods:
            return None
        window = frame.iloc[-periods:]
        if self._too_old(window.index[-1], asof, max_age_days):
            return None
        if periods > 1:
            gaps = window.index.to_series().diff().dt.days.dropna()
            if not bool(gaps.between(270, 460).all()):
                return None
        for concept, count in requirements.items():
            if window[concept].iloc[-count:].isna().any():
                return None
        return window

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

    def _latest_direct(
        self,
        frame: pd.DataFrame,
        concept: str,
        asof: date | None,
        max_age_days: int | None,
    ) -> float | None:
        """Last value of a stored column, subject to the age bound."""
        series = self._dated_series(frame, concept, asof=asof)
        if series is None:
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
    def valuation_price(self) -> float | None:
        """Return a price that is safe to use as an exact valuation input.

        A price inferred from unadjusted public float is only a lower bound because affiliate
        holdings are excluded.  Basis contradictions are also quarantined.  The raw observation
        remains available in metadata for audit, but it must not feed market cap or multiples.
        """
        if self.meta.get("price_source") == "public_float_lower_bound":
            return None
        if self.meta.get("valuation_price_rejected"):
            return None
        if self.price is None or self.price <= 0:
            return None
        return self.price

    @property
    def market_cap(self) -> float | None:
        """Price times shares, or ``None`` if either is not a usable positive number.

        A negative or zero price is a data error, never a quotation. Left unchecked it produced a
        negative market cap, and since every valuation multiple guards its *denominator* rather
        than its numerator, the result was a clean 0.0 — the best possible score — at full
        reported coverage.
        """
        price = self.valuation_price
        if price is None or self.shares_outstanding is None:
            return None
        if self.shares_outstanding <= 0:
            return None
        return price * self.shares_outstanding

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
    """The final artefact: a grade, its evidence, refusal gates, and model stability."""

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
    gates: list[str] = field(default_factory=list)  # hard gates that refuse the letter as N/A
    explain: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def graded(self) -> bool:
        """False when coverage was too low to responsibly issue a grade."""
        return self.letter != "N/A"

    @property
    def sensitivity_interval(self) -> tuple[float, float] | None:
        """Model-sensitivity interval, retained in :attr:`ci` for API compatibility.

        The interval perturbs weights and metric inclusion. It is not a predictive interval for
        the stock price or future return, so new interfaces should prefer this truthful name while
        older consumers can continue reading ``ci`` unchanged.
        """
        return self.ci

    @property
    def letter_probabilities(self) -> dict[str, float]:
        """Compatibility alias for letter frequencies under model perturbation scenarios."""
        values = self.explain.get(
            "letter_scenario_frequencies",
            self.explain.get("letter_probabilities", {}),
        )
        if not isinstance(values, dict):
            return {}
        probabilities: dict[str, float] = {}
        for letter, probability in values.items():
            try:
                probabilities[str(letter)] = float(probability)
            except (TypeError, ValueError):
                continue
        return probabilities

    def top_contributors(self, n: int = 5, *, positive: bool = True) -> list[tuple[str, float]]:
        """Metrics that moved the grade most, signed relative to a neutral 50 score."""
        exact = self.explain.get("metric_contributions", {})
        if isinstance(exact, dict) and exact:
            exact_contributions: list[tuple[str, float]] = []
            for metric, contribution in exact.items():
                try:
                    value = float(contribution)
                except (TypeError, ValueError):
                    continue
                if (positive and value > 0.0) or (not positive and value < 0.0):
                    exact_contributions.append((str(metric), value))
            exact_contributions.sort(key=lambda item: item[1], reverse=positive)
            return exact_contributions[:n]
        flat: list[tuple[str, float]] = []
        pillar_weights = self.effective_pillar_weights or self.pillar_weights
        for pillar, ps in self.pillars.items():
            pw = pillar_weights.get(pillar, 0.0)
            for metric, contribution in ps.contributions.items():
                value = contribution * pw
                if (positive and value > 0.0) or (not positive and value < 0.0):
                    flat.append((metric, value))
        flat.sort(key=lambda kv: kv[1], reverse=positive)
        return flat[:n]
