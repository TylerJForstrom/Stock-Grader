"""The grading pipeline: snapshots in, graded reports out.

Orchestrates every other module in one place::

    snapshots -> metrics -> normalize (cross-section) -> weight (level 1) -> pillars
              -> weight (level 2) -> composite -> letter + interval + explanation

Two structural decisions worth stating:

**Normalisation is cross-sectional, so the whole universe is graded at once.** Grading one stock in
isolation and grading it inside a universe are different operations, and the second is the one that
means something. :func:`grade_universe` is therefore the primitive; :func:`grade_one` is a thin
wrapper that grades a single name against whatever peers it was given, falling back to the absolute
piecewise anchors when it has none.

**Weighting happens twice, with the same registry.** The metric-level call sees a matrix of
securities by metrics within one pillar; the pillar-level call sees securities by pillars. That the
same twenty methods apply at both levels is the point of the design — the "weight the aspects, then
weight the attributes" structure is one mechanism used twice, not two mechanisms.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

from .aggregate import aggregate
from .metrics.engine import evaluate_metrics
from .normalize import NEUTRAL_SCORE, normalize_series
from .registry import METRICS
from .scoring import (
    build_pillar_score,
    coverage_penalty,
    explain_contributions,
    grade_from_percentile,
    hybrid_grade,
    to_letter,
    uncertainty_interval,
)
from .types import Coverage, GradeReport, MetricResult, SecuritySnapshot
from .weighting import WeightingContext, compute_weights

log = logging.getLogger(__name__)

__all__ = ["GradeConfig", "grade_universe", "grade_one", "build_metric_matrix"]

MIN_COVERAGE_TO_GRADE = 0.35


class GradeConfig:
    """Everything that can be varied about how a grade is produced.

    A profile is just a preset of this object, which is why adding an investment style needs no new
    code — see :mod:`stock_grader.profiles`.
    """

    def __init__(
        self,
        *,
        name: str = "all_weather",
        pillar_weights: dict[str, float] | None = None,
        metric_weights: dict[str, dict[str, float]] | None = None,
        metric_whitelist: list[str] | None = None,
        normalizer: str = "robust_z",
        metric_weighting: str = "equal",
        pillar_weighting: str = "fixed",
        metric_aggregator: str = "weighted_mean",
        pillar_aggregator: str = "ces",
        aggregator_kwargs: dict | None = None,
        sector_neutral: bool = False,
        curve: str = "hybrid",
        absolute_weight: float = 0.5,
        seed: int = 0,
        uncertainty_draws: int = 300,
        gates: bool = True,
    ) -> None:
        self.name = name
        self.pillar_weights = pillar_weights or {}
        self.metric_weights = metric_weights or {}
        self.metric_whitelist = metric_whitelist
        self.normalizer = normalizer
        self.metric_weighting = metric_weighting
        self.pillar_weighting = pillar_weighting
        self.metric_aggregator = metric_aggregator
        self.pillar_aggregator = pillar_aggregator
        self.aggregator_kwargs = aggregator_kwargs or {"rho": 0.5}
        self.sector_neutral = sector_neutral
        self.curve = curve
        self.absolute_weight = absolute_weight
        self.seed = seed
        self.uncertainty_draws = uncertainty_draws
        self.gates = gates


def build_metric_matrix(
    snapshots: list[SecuritySnapshot],
    *,
    names: list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, dict[str, MetricResult]]]:
    """Evaluate every metric for every security.

    Returns the raw-value matrix (securities x metrics) and the full ``MetricResult`` objects,
    which carry the coverage state the matrix alone cannot express: a NaN in the matrix could mean
    "missing" or "not applicable", and those are treated differently downstream.
    """
    rows: dict[str, dict[str, float | None]] = {}
    results: dict[str, dict[str, MetricResult]] = {}
    for snapshot in snapshots:
        evaluated = evaluate_metrics(snapshot, names=names)
        results[snapshot.ticker] = evaluated
        rows[snapshot.ticker] = {
            name: (result.value if result.coverage is Coverage.OK else np.nan)
            for name, result in evaluated.items()
        }
    matrix = pd.DataFrame.from_dict(rows, orient="index").astype("float64")
    return matrix, results


def _normalize_matrix(
    matrix: pd.DataFrame,
    snapshots: list[SecuritySnapshot],
    config: GradeConfig,
) -> pd.DataFrame:
    """Convert raw metric values to 0..100 scores, one cross-section at a time."""
    sectors = (
        pd.Series({s.ticker: s.sector.value for s in snapshots})
        if config.sector_neutral
        else None
    )
    scored: dict[str, pd.Series] = {}
    for name in matrix.columns:
        spec = METRICS.maybe(name)
        if spec is None:
            continue
        scored[name] = normalize_series(
            matrix[name],
            method=config.normalizer,
            direction=spec.direction,
            sectors=sectors,
            ideal_band=spec.ideal_band,
            anchors=None,
        )
    return pd.DataFrame(scored, index=matrix.index) if scored else pd.DataFrame(index=matrix.index)


def _pillar_members(names: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for name in names:
        spec = METRICS.maybe(name)
        if spec is not None:
            grouped[spec.pillar].append(name)
    return dict(grouped)


def _curve_interval(
    interval: tuple[float, float],
    percentile: float | None,
    config: GradeConfig,
) -> tuple[float, float]:
    """Map a raw-composite confidence interval onto whatever scale the headline score uses.

    Under the ``hybrid`` curve the reported score is a blend of the raw composite and the security's
    percentile. The percentile is a fixed property of the security within this universe, so only the
    raw half of the blend carries uncertainty and the bounds transform with the same affine map.
    """
    low, high = interval
    if not np.isfinite(low) or not np.isfinite(high):
        return interval
    if config.curve == "absolute" or percentile is None or not np.isfinite(percentile):
        return interval
    if config.curve == "cross_sectional":
        # The letter comes purely from rank, which the metric-level resampling does not perturb;
        # the raw interval is still the honest statement about the underlying score.
        return interval
    weight = config.absolute_weight
    return (
        float(np.clip(weight * low + (1 - weight) * percentile, 0.0, 100.0)),
        float(np.clip(weight * high + (1 - weight) * percentile, 0.0, 100.0)),
    )


def grade_universe(
    snapshots: list[SecuritySnapshot],
    config: GradeConfig | None = None,
    *,
    forward_returns: pd.Series | None = None,
    previous_letters: dict[str, str] | None = None,
) -> dict[str, GradeReport]:
    """Grade every security against the others. The primitive operation.

    Args:
        forward_returns: enables the supervised weighting methods. Supplying realised returns that
            overlap the metric measurement window is look-ahead bias; the validation harness
            constructs these correctly, and callers passing them by hand must lag them themselves.
        previous_letters: prior grades, used for boundary hysteresis so trivial score movement does
            not flip a letter.
    """
    config = config or GradeConfig()
    if not snapshots:
        return {}

    matrix, results = build_metric_matrix(snapshots, names=config.metric_whitelist)
    if matrix.empty:
        log.warning("no metrics registered or evaluated; returning ungraded reports")
        return {
            s.ticker: GradeReport(
                ticker=s.ticker, asof=s.asof, profile=config.name, score=float("nan"),
                letter="N/A", warnings=["no metrics available"],
            )
            for s in snapshots
        }

    scores = _normalize_matrix(matrix, snapshots, config)
    pillars = _pillar_members(list(scores.columns))
    warnings_by_ticker: dict[str, list[str]] = {s.ticker: list(s.warnings) for s in snapshots}

    # ---- Level 1: metrics -> pillars, one weighting computation per pillar (shared across the
    # universe, because the weights describe the metric set, not any individual security).
    pillar_scores: dict[str, pd.Series] = {}
    pillar_objects: dict[str, dict[str, object]] = defaultdict(dict)

    for pillar, members in sorted(pillars.items()):
        block = scores[members]
        ctx = WeightingContext(
            forward_returns=forward_returns,
            fixed=config.metric_weights.get(pillar),
            config={"seed": config.seed},
            seed=config.seed,
        )
        method = "fixed" if config.metric_weights.get(pillar) else config.metric_weighting
        weights = compute_weights(block, method=method, ctx=ctx)

        column: dict[str, float] = {}
        for ticker in block.index:
            per_metric = block.loc[ticker]
            member_results = {m: results[ticker][m] for m in members if m in results[ticker]}
            pillar_score = build_pillar_score(
                pillar,
                member_results,
                per_metric,
                weights,
                aggregator=config.metric_aggregator,
                weighting_method=method,
                warnings=ctx.warnings,
            )
            pillar_objects[ticker][pillar] = pillar_score
            column[ticker] = pillar_score.score
        pillar_scores[pillar] = pd.Series(column, dtype="float64")
        for ticker in block.index:
            warnings_by_ticker[ticker].extend(w for w in ctx.warnings if w not in warnings_by_ticker[ticker])

    pillar_matrix = pd.DataFrame(pillar_scores)
    if pillar_matrix.empty:
        return {}

    # Metrics no security in the universe could compute are a limitation of this *run* — no price
    # feed was reachable, say — not a gap in any one company's disclosure. Counting them against
    # every company's coverage would drag the whole universe below the grading threshold and
    # produce a table of N/A grades that says nothing about the companies.
    universe_computable = {
        name
        for name in matrix.columns
        if any(
            results[t].get(name) is not None and results[t][name].coverage is Coverage.OK
            for t in results
        )
    }
    run_limited = sorted(set(matrix.columns) - universe_computable)

    # ---- Level 2: pillars -> composite. Same registry, one level up.
    top_ctx = WeightingContext(
        forward_returns=forward_returns,
        fixed=config.pillar_weights or None,
        config={"seed": config.seed},
        seed=config.seed,
    )
    top_method = config.pillar_weighting if not config.pillar_weights else "fixed"
    pillar_weights = compute_weights(pillar_matrix, method=top_method, ctx=top_ctx)

    composites: dict[str, float] = {}
    for ticker in pillar_matrix.index:
        value = aggregate(
            pillar_matrix.loc[ticker],
            pillar_weights,
            method=config.pillar_aggregator,
            **config.aggregator_kwargs,
        )
        composites[ticker] = float(value) if value is not None else float("nan")
    composite = pd.Series(composites, dtype="float64")

    percentiles = composite.rank(pct=True) * 100.0 if composite.notna().sum() > 1 else None

    # ---- Assemble reports.
    reports: dict[str, GradeReport] = {}
    for snapshot in snapshots:
        ticker = snapshot.ticker
        # A pillar with no usable metrics is not a zero, it is an absent pillar — for a bank the
        # whole efficiency pillar is structurally undefined. Carrying it as NaN would print a
        # meaningless row and invite it into downstream arithmetic.
        objects = {
            p: obj
            for p, obj in pillar_objects.get(ticker, {}).items()
            if np.isfinite(obj.score)
        }
        # Separate the two very different reasons a pillar can be empty. Reporting "not applicable
        # to a general company" when the real cause is an absent price feed sends the reader
        # looking for a sector bug that does not exist.
        dropped_na, dropped_missing = [], []
        for pillar, obj in pillar_objects.get(ticker, {}).items():
            if pillar in objects:
                continue
            (dropped_na if obj.n_not_applicable > obj.n_missing else dropped_missing).append(pillar)
        score = composite.get(ticker, float("nan"))

        all_results = results.get(ticker, {})
        ok = sum(1 for r in all_results.values() if r.coverage is Coverage.OK)
        missing = sum(1 for r in all_results.values() if r.coverage is Coverage.MISSING)
        # Coverage is measured against what was knowable in this run for anyone, so it compares
        # this company with its peers rather than with an ideal that no security could reach.
        missing_company = sum(
            1
            for name, r in all_results.items()
            if r.coverage is Coverage.MISSING and name in universe_computable
        )
        applicable = ok + missing_company
        coverage = (ok / applicable) if applicable else 0.0

        percentile = float(percentiles[ticker]) if percentiles is not None and ticker in percentiles else None

        if config.curve == "absolute":
            final_score, letter = float(score), to_letter(score)
        elif config.curve == "cross_sectional" and percentile is not None:
            final_score, letter = float(score), grade_from_percentile(percentile)
        else:
            final_score, letter = hybrid_grade(score, percentile, absolute_weight=config.absolute_weight)

        warns = warnings_by_ticker.get(ticker, [])
        # A pillar that computed fine but carries no weight contributes nothing, silently. That is
        # sometimes deliberate — the momentum profile omits valuation on purpose — but it is also
        # how a newly-working pillar goes unnoticed: risk, momentum and liquidity all computed
        # correctly for months while every profile weighted them at zero, because the profiles were
        # written back when those pillars could never fire.
        unweighted = sorted(
            p for p, obj in objects.items()
            if np.isfinite(obj.score) and pillar_weights.get(p, 0.0) <= 0.0
        )
        if unweighted:
            warns.append(
                f"pillar(s) computed but given zero weight by the '{config.name}' profile: "
                f"{', '.join(unweighted)} — they do not affect this grade"
            )
        if dropped_na:
            warns.append(
                f"pillar(s) not defined for a {snapshot.sector.value}: {', '.join(sorted(dropped_na))}"
            )
        if dropped_missing:
            reason = "no price history" if not snapshot.has_prices else "insufficient data"
            warns.append(
                f"pillar(s) skipped ({reason}): {', '.join(sorted(dropped_missing))}"
            )
        if len(snapshots) < 2:
            warns.insert(
                0,
                "SCORED WITHOUT PEERS: cross-sectional metrics have nothing to compare against, so "
                "every pillar defaults to a neutral 50 and this grade carries no information. "
                "Pass --universe with peer tickers for a meaningful grade.",
            )
        if run_limited:
            warns.append(
                f"{len(run_limited)} metric(s) unavailable for the entire universe this run "
                f"(e.g. {', '.join(run_limited[:3])}) — excluded from coverage rather than "
                f"charged against every company"
            )
        if snapshot.synthetic_prices:
            warns.insert(0, "price-derived metrics computed from SYNTHETIC prices, not market history")

        if coverage < MIN_COVERAGE_TO_GRADE or not np.isfinite(score):
            letter = "N/A"
            warns.append(
                f"refusing to grade: only {coverage:.0%} of applicable metrics could be computed "
                f"(need {MIN_COVERAGE_TO_GRADE:.0%})"
            )
        elif len(snapshots) < 2 and config.curve != "absolute":
            # Every cross-sectional score collapsed to the neutral value, so the composite is an
            # artefact of having nothing to compare against. Reporting a letter here would dress
            # up "no information" as a considered C.
            letter = "N/A"

        pillar_series = pd.Series({p: obj.score for p, obj in objects.items()}, dtype="float64")
        raw_interval = uncertainty_interval(
            pillar_series,
            pillar_weights,
            coverage=coverage,
            aggregator=config.pillar_aggregator,
            draws=config.uncertainty_draws,
            seed=config.seed,
            **config.aggregator_kwargs,
        )
        # The interval is estimated on the raw composite, so it has to pass through the same curve
        # the headline score did. Reporting a hybrid-scaled score beside a raw-scaled interval
        # produced intervals that did not contain their own score.
        interval = _curve_interval(raw_interval, percentile, config)

        reports[ticker] = GradeReport(
            ticker=ticker,
            asof=snapshot.asof,
            profile=config.name,
            score=final_score,
            letter=letter,
            pillars=objects,
            pillar_weights={k: float(v) for k, v in pillar_weights.items()},
            percentile=percentile,
            ci=interval,
            coverage=coverage,
            weighting_method=f"{config.metric_weighting}/{top_method}",
            normalizer=config.normalizer,
            aggregator=f"{config.metric_aggregator}/{config.pillar_aggregator}",
            explain={
                "pillar_contributions": explain_contributions(pillar_series, pillar_weights),
                "n_metrics_ok": ok,
                "n_metrics_missing": missing,
                "n_metrics_not_applicable": sum(
                    1 for r in all_results.values() if r.coverage is Coverage.NOT_APPLICABLE
                ),
                "n_metrics_run_limited": len(run_limited),
                "universe_size": len(snapshots),
            },
            warnings=warns,
            meta={
                "sector": snapshot.sector.value,
                "pit_mode": snapshot.meta.get("pit_mode"),
                "curve": config.curve,
                "coverage_penalty": coverage_penalty(coverage),
            },
        )
    return reports


def grade_one(
    snapshot: SecuritySnapshot,
    peers: list[SecuritySnapshot] | None = None,
    config: GradeConfig | None = None,
) -> GradeReport:
    """Grade a single security, optionally against peers.

    With no peers the cross-sectional normalizers have nothing to compare against and every metric
    would score a flat 50, so this is the case the absolute piecewise anchors exist for. The report
    records that it was graded without a peer group, because a gradeless-of-context grade is a
    weaker claim and should say so.
    """
    config = config or GradeConfig()
    universe = [snapshot] + list(peers or [])
    reports = grade_universe(universe, config)
    report = reports.get(snapshot.ticker)
    if report is None:
        return GradeReport(
            ticker=snapshot.ticker, asof=snapshot.asof, profile=config.name,
            score=float("nan"), letter="N/A", warnings=["grading produced no result"],
        )
    if not peers:
        report.warnings.append(
            "graded without a peer universe: cross-sectional scores are not meaningful, "
            "use --universe for a comparable grade"
        )
    return report
