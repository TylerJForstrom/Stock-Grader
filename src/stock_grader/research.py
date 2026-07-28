"""Analyst-facing research dossier built from the grading evidence.

The dossier answers four questions the headline grade cannot:

1. Which peers define the comparison?
2. What raw values sit behind the normalized scores?
3. How has the business changed over the last five fiscal years?
4. What assumptions would be required to justify the current price?

It contains no generated buy/sell language.  A peer-relative historical screen and an
assumption-led valuation are useful inputs to research, not a substitute for a thesis.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from .peers import PeerSelection
from .pipeline import GradeConfig, build_metric_matrix, grade_universe
from .registry import METRICS
from .types import Coverage, GradeReport, SecuritySnapshot
from .valuation import ValuationAnalysis, build_valuation_analysis

__all__ = [
    "MetricEvidence",
    "ResearchReport",
    "TrendSeries",
    "build_research_report",
    "research_to_json",
    "research_to_markdown",
]

SCHEMA_VERSION = "1.0"


@dataclass(slots=True)
class MetricEvidence:
    """Raw, normalized, weighted, and peer-relative views of one metric."""

    name: str
    pillar: str
    description: str
    raw_value: float | None
    unit: str
    coverage: str
    missing_reason: str
    normalized_score: float | None
    metric_weight: float
    effective_pillar_weight: float
    contribution: float
    peer_median: float | None
    peer_q25: float | None
    peer_q75: float | None
    peer_percentile: float | None
    usable_peers: int
    direction: int
    note: str = ""
    raw_inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TrendSeries:
    """One five-year fiscal trend."""

    name: str
    unit: str
    observations: list[dict[str, Any]]


@dataclass(slots=True)
class ResearchReport:
    """Portable evidence bundle for one security."""

    schema_version: str
    company: dict[str, Any]
    grade: GradeReport
    peer_selection: PeerSelection
    provenance: dict[str, Any]
    metrics: list[MetricEvidence]
    trends: list[TrendSeries]
    valuation: ValuationAnalysis
    interpretation: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def _json_safe(value: Any) -> Any:
    if value is pd.NA:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, pd.Series):
        return {
            str(index): _json_safe(item)
            for index, item in value.items()
        }
    if isinstance(value, pd.DataFrame):
        return _json_safe(value.to_dict(orient="index"))
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _midrank_percentile(value: float, peers: pd.Series) -> float | None:
    clean = pd.to_numeric(peers, errors="coerce").dropna()
    if not math.isfinite(value) or clean.empty:
        return None
    population = pd.concat([clean, pd.Series([value], index=["__target__"])])
    ranks = population.rank(method="average")
    # Hazen plotting position: the middle observation maps to 50 and ties share the same rank.
    return float((ranks["__target__"] - 0.5) / len(population) * 100.0)


def _desirability_percentile(
    value: float,
    peers: pd.Series,
    *,
    direction: int,
    ideal_band: tuple[float, float] | None,
) -> float | None:
    """Peer percentile where a larger number always means more desirable."""

    if direction == 0:
        if ideal_band is None:
            return None
        low, high = ideal_band

        def distance(item: float) -> float:
            return max(low - item, 0.0, item - high)

        target_distance = distance(value)
        peer_distances = pd.to_numeric(peers, errors="coerce").dropna().map(distance)
        raw = _midrank_percentile(target_distance, peer_distances)
        return None if raw is None else 100.0 - raw
    raw = _midrank_percentile(value, peers)
    if raw is None:
        return None
    return raw if direction > 0 else 100.0 - raw


def _metric_evidence(
    target: SecuritySnapshot,
    peers: list[SecuritySnapshot],
    report: GradeReport,
) -> list[MetricEvidence]:
    universe = [target, *peers]
    matrix, results = build_metric_matrix(universe)
    target_results = results.get(target.ticker, {})
    evidence: list[MetricEvidence] = []
    exact_contributions = dict(report.explain.get("metric_contributions", {}))

    pillar_by_metric: dict[str, tuple[float, float, float | None]] = {}
    for pillar_name, pillar in report.pillars.items():
        effective_pillar = report.effective_pillar_weights.get(
            pillar_name, report.pillar_weights.get(pillar_name, 0.0)
        )
        for metric_name in target_results:
            if metric_name not in pillar.weights and metric_name not in pillar.metric_scores:
                continue
            fallback_contribution = (
                pillar.contributions.get(metric_name, 0.0) * effective_pillar
            )
            pillar_by_metric[metric_name] = (
                pillar.weights.get(metric_name, 0.0),
                effective_pillar,
                pillar.metric_scores.get(metric_name),
            )
            if metric_name not in exact_contributions:
                exact_contributions[metric_name] = fallback_contribution

    peer_tickers = [peer.ticker for peer in peers if peer.ticker in matrix.index]
    for name, result in target_results.items():
        spec = METRICS.maybe(name)
        metric_weight, pillar_weight, normalized = pillar_by_metric.get(
            name, (0.0, 0.0, None)
        )
        peer_values = (
            pd.to_numeric(matrix.loc[peer_tickers, name], errors="coerce").dropna()
            if name in matrix.columns and peer_tickers
            else pd.Series(dtype="float64")
        )
        raw = float(result.value) if result.value is not None else None
        q25 = float(peer_values.quantile(0.25)) if len(peer_values) else None
        median = float(peer_values.median()) if len(peer_values) else None
        q75 = float(peer_values.quantile(0.75)) if len(peer_values) else None
        evidence.append(
            MetricEvidence(
                name=name,
                pillar=result.pillar,
                description=spec.description if spec is not None else "",
                raw_value=raw,
                unit=result.unit or (spec.unit if spec is not None else ""),
                coverage=result.coverage.value,
                missing_reason=(
                    result.note
                    if result.coverage is not Coverage.OK
                    else ""
                ),
                normalized_score=float(normalized) if normalized is not None else None,
                metric_weight=float(metric_weight),
                effective_pillar_weight=float(pillar_weight),
                contribution=float(exact_contributions.get(name, 0.0)),
                peer_median=median,
                peer_q25=q25,
                peer_q75=q75,
                peer_percentile=(
                    _desirability_percentile(
                        raw,
                        peer_values,
                        direction=spec.direction if spec is not None else result.direction,
                        ideal_band=spec.ideal_band if spec is not None else None,
                    )
                    if raw is not None
                    else None
                ),
                usable_peers=len(peer_values),
                direction=spec.direction if spec is not None else result.direction,
                note=result.note,
                raw_inputs=_json_safe(result.raw_inputs),
            )
        )
    return sorted(evidence, key=lambda item: (item.pillar, item.name))


def _ratio_series(
    frame: pd.DataFrame,
    numerator: str,
    denominator: str,
) -> pd.Series:
    if numerator not in frame or denominator not in frame:
        return pd.Series(dtype="float64")
    denominator_values = pd.to_numeric(frame[denominator], errors="coerce").replace(0.0, np.nan)
    return pd.to_numeric(frame[numerator], errors="coerce") / denominator_values


def _trend(name: str, unit: str, values: pd.Series) -> TrendSeries | None:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return None
    clean = clean.sort_index().iloc[-5:]
    return TrendSeries(
        name=name,
        unit=unit,
        observations=[
            {"period": pd.Timestamp(index).date().isoformat(), "value": float(value)}
            for index, value in clean.items()
        ],
    )


def _trends(snapshot: SecuritySnapshot) -> list[TrendSeries]:
    if snapshot.fundamentals is None or snapshot.fundamentals.annual.empty:
        return []
    annual = snapshot.fundamentals.annual
    candidates: list[TrendSeries | None] = []
    for concept, label, unit in (
        ("revenue", "revenue", snapshot.currency),
        ("net_income", "net income", snapshot.currency),
        ("cfo", "cash from operations", snapshot.currency),
        ("fcf", "free cash flow", snapshot.currency),
        ("total_debt", "total debt", snapshot.currency),
        ("equity", "book equity", snapshot.currency),
        ("shares_diluted", "diluted shares", "shares"),
    ):
        if concept in annual:
            candidates.append(_trend(label, unit, annual[concept]))
    for name, numerator, denominator in (
        ("gross margin", "gross_profit", "revenue"),
        ("operating margin", "operating_income", "revenue"),
        ("net margin", "net_income", "revenue"),
        ("free cash flow margin", "fcf", "revenue"),
        ("return on assets (ending balance)", "net_income", "assets"),
    ):
        candidates.append(_trend(name, "ratio", _ratio_series(annual, numerator, denominator)))
    return [item for item in candidates if item is not None]


def _latest_filing(snapshot: SecuritySnapshot) -> str | None:
    fundamentals = snapshot.fundamentals
    if fundamentals is None or fundamentals.filed.empty:
        return None
    values = pd.to_datetime(fundamentals.filed, errors="coerce").dropna()
    values = values[values <= pd.Timestamp(snapshot.asof)]
    return values.max().date().isoformat() if not values.empty else None


def _latest_period(snapshot: SecuritySnapshot) -> str | None:
    fundamentals = snapshot.fundamentals
    if fundamentals is None:
        return None
    indexes: list[pd.Timestamp] = []
    for frame in (fundamentals.quarterly, fundamentals.annual):
        if not frame.empty:
            eligible = pd.to_datetime(frame.index, errors="coerce")
            eligible = eligible[eligible <= pd.Timestamp(snapshot.asof)]
            indexes.extend(item for item in eligible if not pd.isna(item))
    return max(indexes).date().isoformat() if indexes else None


def _interpretation(report: GradeReport) -> str:
    if report.letter == "N/A":
        return "not rated: evidence or required profile coverage was insufficient"
    interval = report.ci
    width = interval[1] - interval[0] if interval and all(np.isfinite(interval)) else None
    if width is not None and width >= 30:
        return "low-stability screen: the result changes materially under model perturbations"
    if report.letter.startswith(("A", "B")):
        return "positive peer-relative screen; requires qualitative and forward-looking diligence"
    if report.letter.startswith("C"):
        return "mixed peer-relative screen"
    return "negative peer-relative screen; investigate weak pillars and data caveats"


def build_research_report(
    target: SecuritySnapshot,
    peers: list[SecuritySnapshot],
    peer_selection: PeerSelection,
    config: GradeConfig | None = None,
    *,
    valuation_growth_rates: tuple[float, float, float] = (-0.02, 0.05, 0.12),
    valuation_discount_rate: float = 0.10,
    valuation_terminal_growth: float = 0.025,
) -> ResearchReport:
    """Grade a target against selected peers and assemble a complete evidence bundle."""

    if target.ticker.upper() not in {peer_selection.target.upper()}:
        raise ValueError("peer selection target does not match research target")
    reports = grade_universe([target, *peers], config or GradeConfig())
    report = reports.get(target.ticker)
    if report is None:
        raise ValueError(f"grading produced no report for {target.ticker}")

    provenance = {
        "asof": target.asof.isoformat(),
        "pit_mode": target.meta.get("pit_mode"),
        "latest_fiscal_period": _latest_period(target),
        "latest_filing_date": _latest_filing(target),
        "price_source": target.meta.get("price_source"),
        "price_date": target.meta.get("price_date"),
        "price_age_days": target.meta.get("price_age_days"),
        "price_lower_bound": target.meta.get("price_lower_bound"),
        "valuation_price_rejected": target.meta.get("valuation_price_rejected"),
        "price_share_basis_check": _json_safe(target.meta.get("price_share_basis_check")),
        "yahoo_share_basis_reconciliation": _json_safe(
            target.meta.get("yahoo_share_basis_reconciliation")
        ),
        "price_rejections": _json_safe(target.meta.get("price_rejections", [])),
        "price_is_adjusted": bool(target.meta.get("price_is_adjusted", False)),
        "shares_source": target.meta.get("shares_source", "SEC cover page"),
        "shares_date": _json_safe(target.meta.get("shares_date")),
        "benchmark": target.meta.get("benchmark"),
        "benchmark_is_price_only": bool(target.meta.get("benchmark_is_price_only", False)),
        "curve": report.meta.get("curve"),
        "score_interpretation": report.meta.get("score_interpretation"),
        "config_fingerprint": report.meta.get("config_fingerprint"),
        "universe_fingerprint": report.meta.get("universe_fingerprint"),
        "effective_peer_count": report.meta.get("effective_peer_count"),
        "canonical_concept_tags": (
            dict(target.fundamentals.tag_used) if target.fundamentals is not None else {}
        ),
        "peer_fingerprint": peer_selection.fingerprint,
    }
    valuation = build_valuation_analysis(
        target,
        growth_rates=valuation_growth_rates,
        discount_rate=valuation_discount_rate,
        terminal_growth_rate=valuation_terminal_growth,
    )
    warnings = list(
        dict.fromkeys(
            [
                *peer_selection.warnings,
                *report.warnings,
                *valuation.warnings,
                "Stock-Grader is a research screen, not investment advice or a prediction of returns.",
            ]
        )
    )
    return ResearchReport(
        schema_version=SCHEMA_VERSION,
        company={
            "ticker": target.ticker,
            "name": target.name,
            "cik": target.cik,
            "sic": target.sic,
            "industry": target.industry,
            "business_model": target.sector.value,
            "currency": target.currency,
            "price": target.price,
            "shares_outstanding": target.shares_outstanding,
            "market_cap": target.market_cap,
        },
        grade=report,
        peer_selection=peer_selection,
        provenance=provenance,
        metrics=_metric_evidence(target, peers, report),
        trends=_trends(target),
        valuation=valuation,
        interpretation=_interpretation(report),
        warnings=warnings,
    )


def research_to_json(report: ResearchReport, *, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


def _fmt(value: float | None, unit: str = "") -> str:
    if value is None or not math.isfinite(value):
        return "—"
    if unit in ("ratio", "%"):
        return f"{value:.1%}"
    if unit == "shares":
        return f"{value:,.0f}"
    if unit and unit not in ("x", "days"):
        return f"{value:,.2f} {unit}"
    suffix = f" {unit}" if unit else ""
    return f"{value:,.3f}{suffix}"


def research_to_markdown(report: ResearchReport) -> str:
    """Render a complete, non-truncated Markdown research dossier."""

    grade = report.grade
    company = report.company
    lines = [
        f"# {company['ticker']} — quantitative research dossier",
        "",
        (
            f"**{company.get('name') or company['ticker']}** · "
            f"{company.get('industry') or company.get('business_model')} · "
            f"as of {report.provenance.get('asof')}"
        ),
        "",
        f"> {report.interpretation}. This is a research screen, not investment advice.",
        "",
        "## Company snapshot",
        "",
        "| field | value |",
        "|---|---:|",
        f"| Current price | {_fmt(company.get('price'), company.get('currency') or '')} |",
        f"| Shares outstanding | {_fmt(company.get('shares_outstanding'), 'shares')} |",
        f"| Market capitalization | {_fmt(company.get('market_cap'), company.get('currency') or '')} |",
        "",
        "## Executive screen",
        "",
        "| grade | peer score | model sensitivity range | coverage | peer percentile |",
        "|---:|---:|---:|---:|---:|",
        (
            f"| {grade.letter} | {grade.score:.1f} | "
            f"{f'{grade.ci[0]:.1f}–{grade.ci[1]:.1f}' if grade.ci else '—'} | "
            f"{grade.coverage:.0%} | "
            f"{f'{grade.percentile:.1f}' if grade.percentile is not None else '—'} |"
        ),
    ]
    frequencies = grade.explain.get("letter_scenario_frequencies", {})
    if frequencies:
        rendered = ", ".join(
            f"{letter} {frequency:.0%}"
            for letter, frequency in sorted(
                frequencies.items(), key=lambda item: (-item[1], item[0])
            )
        )
        lines += ["", f"Letter frequencies across model perturbations: {rendered}."]
    if grade.gates:
        lines += ["", "Grading gates: " + ", ".join(grade.gates) + "."]
    lines += [
        "",
        "## Data provenance",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key in (
        "pit_mode",
        "latest_fiscal_period",
        "latest_filing_date",
        "price_source",
        "price_date",
        "price_age_days",
        "shares_source",
        "shares_date",
        "benchmark",
        "curve",
        "score_interpretation",
        "effective_peer_count",
        "config_fingerprint",
        "universe_fingerprint",
        "peer_fingerprint",
    ):
        lines.append(f"| {key.replace('_', ' ')} | {report.provenance.get(key) or '—'} |")

    lines += [
        "",
        "## Comparable companies",
        "",
        f"Rule: {report.peer_selection.rule}",
        "",
        f"Members ({len(report.peer_selection.members)}): "
        + ", ".join(report.peer_selection.members),
    ]
    if report.peer_selection.widening_steps:
        lines += ["", "Widening record: " + "; ".join(report.peer_selection.widening_steps)]

    lines += [
        "",
        "## Pillars",
        "",
        "| pillar | score | effective weight | coverage |",
        "|---|---:|---:|---:|",
    ]
    for name, pillar in sorted(grade.pillars.items(), key=lambda item: -item[1].score):
        lines.append(
            f"| {name} | {pillar.score:.1f} | "
            f"{grade.effective_pillar_weights.get(name, 0.0):.1%} | {pillar.coverage:.0%} |"
        )

    available_metrics = [
        metric for metric in report.metrics if metric.coverage == Coverage.OK.value
    ]
    available_metrics.sort(key=lambda metric: abs(metric.contribution), reverse=True)
    lines += [
        "",
        "## Highest-impact metric evidence",
        "",
        "| metric | raw | normalized | contribution | desirability pct | peer median | peer IQR | n |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in available_metrics:
        iqr = (
            f"{_fmt(metric.peer_q25, metric.unit)} – {_fmt(metric.peer_q75, metric.unit)}"
            if metric.peer_q25 is not None and metric.peer_q75 is not None
            else "—"
        )
        lines.append(
            f"| {metric.name} | {_fmt(metric.raw_value, metric.unit)} | "
            f"{_fmt(metric.normalized_score)} | {metric.contribution:+.3f} | "
            f"{_fmt(metric.peer_percentile)} | "
            f"{_fmt(metric.peer_median, metric.unit)} | {iqr} | {metric.usable_peers} |"
        )

    unavailable_metrics = [
        metric for metric in report.metrics if metric.coverage != Coverage.OK.value
    ]
    if unavailable_metrics:
        lines += [
            "",
            "## Missing and not-applicable evidence",
            "",
            "| metric | pillar | status | reason |",
            "|---|---|---|---|",
        ]
        for metric in unavailable_metrics:
            reason = (metric.missing_reason or metric.note or "not supplied").replace("|", "\\|")
            lines.append(
                f"| {metric.name} | {metric.pillar} | {metric.coverage} | {reason} |"
            )

    if report.trends:
        periods = sorted(
            {
                observation["period"]
                for trend in report.trends
                for observation in trend.observations
            }
        )
        lines += [
            "",
            "## Five-year reported trends",
            "",
            "| series | " + " | ".join(periods) + " |",
            "|---|" + "|".join("---:" for _ in periods) + "|",
        ]
        for trend in report.trends:
            values = {item["period"]: item["value"] for item in trend.observations}
            lines.append(
                f"| {trend.name} | "
                + " | ".join(_fmt(values.get(period), trend.unit) for period in periods)
                + " |"
            )

    lines += ["", "## Illustrative cash-flow proxy valuation", ""]
    if report.valuation.available:
        lines += [
            (
                "Uses CFO minus capex as a levered proxy (not canonical FCFE because debt "
                "principal flows are excluded), discounted at a required equity return. "
                "Scenario growth rates are assumptions, not forecasts."
            ),
            "",
            "| scenario | 5y growth | required return | terminal growth | value/share | vs price |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for item in report.valuation.scenarios:
            lines.append(
                f"| {item.scenario.name} | {item.scenario.growth_rate:.1%} | "
                f"{item.scenario.discount_rate:.1%} | "
                f"{item.scenario.terminal_growth_rate:.1%} | "
                f"{item.value_per_share:,.2f} | "
                f"{f'{item.upside_downside:+.1%}' if item.upside_downside is not None else '—'} |"
            )
        implied = report.valuation.implied_five_year_growth
        lines += [
            "",
            (
                "Reverse DCF implied five-year FCF growth: "
                + (f"**{implied:.1%}**" if implied is not None else "**outside search range**")
            ),
        ]
    else:
        lines.append(
            report.valuation.warnings[0]
            if report.valuation.warnings
            else "Valuation unavailable."
        )

    if report.warnings:
        lines += ["", "## Risks, limitations, and data caveats", ""]
        lines += [f"- {warning}" for warning in report.warnings]
    return "\n".join(lines)
