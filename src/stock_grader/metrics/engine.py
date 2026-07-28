"""Metric evaluation engine.

Runs every registered metric against a snapshot and resolves each to one of three states. The
three-way distinction is the point of this module:

``OK``
    Computed.
``MISSING``
    Applicable to this business, but the inputs were not available. Counts against data coverage
    and widens the model-sensitivity range — the grade genuinely knows less about this company.
``NOT_APPLICABLE``
    Not a defined quantity for this business model. A bank has no current ratio; that is not a gap
    in our data, it is a property of bank accounting. Renormalised away with **no** coverage
    penalty, because charging one would hand every financial in the universe an artificially
    uncertain grade for no reason.

A metric function that raises is caught and recorded as ``MISSING`` with the exception text. One
bad metric out of a hundred and fifty must not take down a grade, and a silent ``except: pass``
would hide a real bug — so the failure is captured and surfaced in the report.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from ..data.sectors import is_applicable
from ..registry import METRICS, MetricSpec
from ..types import Coverage, MetricResult, SectorClass, SecuritySnapshot
from .util import is_finite_number

log = logging.getLogger(__name__)

__all__ = ["MIN_HISTORY_DEFAULT", "coverage_summary", "evaluate_metrics", "evaluate_one"]

MIN_HISTORY_DEFAULT = 60  # trading days below which price-derived statistics are unreliable


def evaluate_one(spec: MetricSpec, snapshot: SecuritySnapshot) -> MetricResult:
    """Evaluate a single metric, resolving its coverage state."""
    result = MetricResult(
        name=spec.name,
        pillar=spec.pillar,
        value=None,
        direction=spec.direction,
        unit=spec.unit,
    )

    if spec.name in {"altman_z", "altman_z_prime"}:
        # Applicability depends on SIC within the broad GENERAL sector: the original Z model is
        # for manufacturers, while Z'' is the non-manufacturer variant.  Returning None from the
        # inactive function is not a data gap and must not reduce coverage.
        from .fundamental import _altman_model_for

        expected = {"altman_z": "z", "altman_z_prime": "z_prime"}[spec.name]
        actual = _altman_model_for(snapshot)
        structurally_unsupported = snapshot.sector in {
            SectorClass.BANK,
            SectorClass.INSURANCE,
            SectorClass.REIT,
            SectorClass.HOLDING,
            SectorClass.UTILITY,
        }
        if actual != expected and (actual is not None or structurally_unsupported):
            result.coverage = Coverage.NOT_APPLICABLE
            result.note = (
                f"inactive Altman variant for SIC {snapshot.sic or 'unknown'} "
                f"(applicable variant: {actual or 'none'})"
            )
            return result

    if not is_applicable(spec.name, spec.pillar, snapshot.sector):
        result.coverage = Coverage.NOT_APPLICABLE
        result.note = f"not defined for {snapshot.sector.value}"
        return result

    valuation_rejection = snapshot.meta.get("valuation_price_rejected")
    if spec.pillar == "valuation" and valuation_rejection:
        result.coverage = Coverage.MISSING
        result.note = {
            "public_float_lower_bound": (
                "exact valuation unavailable: SEC public-float price is only a lower bound"
            ),
            "split_basis_mismatch": (
                "exact valuation unavailable: price and share count use incompatible split bases"
            ),
            "split_basis_unverified": (
                "exact valuation unavailable: historical price/share split basis is unverified"
            ),
        }.get(str(valuation_rejection), f"valuation price rejected: {valuation_rejection}")
        result.raw_inputs = {
            "valuation_price_rejected": valuation_rejection,
            "price_source": snapshot.meta.get("price_source"),
            "price_lower_bound": snapshot.meta.get("price_lower_bound"),
            "rejected_dense_price": snapshot.meta.get("rejected_dense_price"),
            "price_share_basis_check": snapshot.meta.get("price_share_basis_check"),
        }
        return result

    if spec.needs_prices and not snapshot.has_prices:
        result.coverage = Coverage.MISSING
        result.note = "no price history available"
        return result

    if spec.needs_prices and spec.min_history:
        available = len(snapshot.prices) if snapshot.prices is not None else 0
        required = max(spec.min_history, MIN_HISTORY_DEFAULT)
        if available < required:
            result.coverage = Coverage.MISSING
            result.note = f"needs {required} trading days, have {available}"
            return result

    if spec.needs_benchmark and snapshot.benchmark is None:
        result.coverage = Coverage.MISSING
        result.note = "no benchmark series available"
        return result

    if spec.needs_risk_free and (snapshot.risk_free is None or snapshot.risk_free.empty):
        # Substituting 0% is not a conservative default, it is a different and flattering
        # statistic. The bias is rf/sigma * sqrt(252), so at a 5.3% T-bill a 25%-vol name gains
        # 0.21 of Sharpe while a 60%-vol name gains 0.09 — vol-dependent, therefore rank-changing
        # on a cross-section whose Sharpe dispersion is around 0.7.
        if not snapshot.meta.get("assume_zero_risk_free"):
            result.coverage = Coverage.MISSING
            result.note = "no risk-free rate available (set assume_zero_risk_free to override)"
            return result

    if snapshot.fundamentals is None and not spec.needs_prices:
        result.coverage = Coverage.MISSING
        result.note = "no fundamentals available"
        return result

    try:
        value = spec.fn(snapshot)
    except Exception as exc:
        log.debug("metric %s raised for %s: %s", spec.name, snapshot.ticker, exc)
        result.coverage = Coverage.MISSING
        result.note = f"error: {type(exc).__name__}: {exc}"
        return result

    # A metric may return a bare float or (value, raw_inputs) for auditability.
    raw: dict = {}
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict):
        value, raw = value

    if not is_finite_number(value):
        result.coverage = Coverage.MISSING
        result.note = result.note or "inputs unavailable"
        result.raw_inputs = raw
        return result

    value = float(value)
    if spec.winsor is not None:
        low, high = spec.winsor
        clamped = max(low, min(high, value))
        if clamped != value:
            result.note = f"clamped from {value:.4g} to plausible range [{low:g}, {high:g}]"
            value = clamped

    result.value = value
    result.raw_inputs = raw
    result.coverage = Coverage.OK
    return result


def evaluate_metrics(
    snapshot: SecuritySnapshot,
    *,
    names: Iterable[str] | None = None,
    pillars: Iterable[str] | None = None,
) -> dict[str, MetricResult]:
    """Evaluate the registered metrics against one snapshot.

    Args:
        names: restrict to these metric names; ``None`` means every registered metric.
        pillars: restrict to these pillars.
    """
    if names is not None:
        specs = [METRICS.get(n) for n in names]
    else:
        specs = [spec for _, spec in METRICS.items()]
    if pillars is not None:
        wanted = set(pillars)
        specs = [s for s in specs if s.pillar in wanted]

    return {spec.name: evaluate_one(spec, snapshot) for spec in specs}


def coverage_summary(results: dict[str, MetricResult]) -> dict[str, object]:
    """Aggregate coverage across evaluated metrics.

    ``coverage`` is computed over *applicable* metrics only, so a bank is not penalised for the
    dozens of manufacturing ratios that were never meaningful for it.
    """
    total = len(results)
    ok = sum(1 for r in results.values() if r.coverage is Coverage.OK)
    missing = sum(1 for r in results.values() if r.coverage is Coverage.MISSING)
    not_applicable = sum(1 for r in results.values() if r.coverage is Coverage.NOT_APPLICABLE)
    applicable = ok + missing

    by_pillar: dict[str, dict[str, int]] = {}
    for result in results.values():
        bucket = by_pillar.setdefault(result.pillar, {"ok": 0, "missing": 0, "not_applicable": 0})
        bucket[result.coverage.value] += 1

    return {
        "total": total,
        "ok": ok,
        "missing": missing,
        "not_applicable": not_applicable,
        "applicable": applicable,
        "coverage": (ok / applicable) if applicable else 0.0,
        "by_pillar": by_pillar,
        "missing_names": sorted(n for n, r in results.items() if r.coverage is Coverage.MISSING),
    }
