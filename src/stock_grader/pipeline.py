"""The grading pipeline: snapshots in, graded reports out.

Orchestrates every other module in one place::

    snapshots -> metrics -> normalize (cross-section) -> weight (level 1) -> pillars
              -> weight (level 2) -> composite -> letter + interval + explanation

Two structural decisions worth stating:

**Normalisation is cross-sectional, so the whole universe is graded at once.** Grading one stock in
isolation and grading it inside a universe are different operations, and the second is the one that
means something. :func:`grade_universe` is therefore the primitive; :func:`grade_one` is a thin
wrapper that grades a single name against whatever peers it was given and refuses a rank-based
letter when no usable comparison set exists.

**Weighting happens twice, with the same registry.** The metric-level call sees a matrix of
securities by metrics within one pillar; the pillar-level call sees securities by pillars. That the
same twenty methods apply at both levels is the point of the design — the "weight the aspects, then
weight the attributes" structure is one mechanism used twice, not two mechanisms.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict

import numpy as np
import pandas as pd

from .aggregate import aggregate
from .metrics.engine import evaluate_metrics
from .normalize import normalize_series
from .registry import AGGREGATORS, METRICS, NORMALIZERS, WEIGHTINGS
from .scoring import (
    PERCENTILE_CUTOFFS,
    apply_hysteresis,
    build_pillar_score,
    coverage_penalty,
    explain_aggregate_contributions,
    grade_from_percentile,
    hazen_percentile,
    hybrid_grade,
    to_letter,
    uncertainty_interval,
)
from .types import Coverage, GradeReport, MetricResult, SecuritySnapshot
from .weighting import WEIGHT_METHOD_INFO, WeightingContext, compute_weights

log = logging.getLogger(__name__)

__all__ = ["GradeConfig", "build_metric_matrix", "grade_one", "grade_universe"]

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
        curve: str = "cross_sectional",
        absolute_weight: float = 0.5,
        seed: int = 0,
        uncertainty_draws: int = 300,
        gates: bool = True,
        min_profile_weight_coverage: float = 0.70,
        required_pillars: set[str] | list[str] | tuple[str, ...] | None = None,
        require_defining_pillar: bool = True,
        allow_in_sample_supervised_weighting: bool = False,
    ) -> None:
        self.name = str(name).strip()
        if not self.name:
            raise ValueError("name cannot be empty")
        self.pillar_weights = {
            str(pillar): value for pillar, value in (pillar_weights or {}).items()
        }
        self.metric_weights = {
            str(pillar): {
                str(metric_name): value for metric_name, value in weights.items()
            }
            for pillar, weights in (metric_weights or {}).items()
        }
        if metric_whitelist is not None and isinstance(metric_whitelist, (str, bytes)):
            raise ValueError("metric_whitelist must be a sequence of metric names, not a string")
        self.metric_whitelist = (
            [str(metric_name) for metric_name in metric_whitelist]
            if metric_whitelist is not None
            else None
        )
        if self.metric_whitelist is not None and len(set(self.metric_whitelist)) != len(
            self.metric_whitelist
        ):
            raise ValueError("metric_whitelist contains duplicate metric names")
        self.normalizer = normalizer
        self.metric_weighting = metric_weighting
        self.pillar_weighting = pillar_weighting
        self.metric_aggregator = metric_aggregator
        self.pillar_aggregator = pillar_aggregator
        self.aggregator_kwargs = dict(aggregator_kwargs or {})
        valid_pillars = {spec.pillar for _, spec in METRICS.items()}
        unknown_pillars = sorted(
            (set(self.pillar_weights) | set(self.metric_weights)) - valid_pillars
        )
        if unknown_pillars:
            raise ValueError(
                "weights contain unknown pillars: " + ", ".join(unknown_pillars)
            )
        configured_vectors = [("pillar_weights", self.pillar_weights)] + [
            (f"metric_weights[{pillar!r}]", values)
            for pillar, values in self.metric_weights.items()
        ]
        for label, weights in configured_vectors:
            invalid: list[str] = []
            for key, value in list(weights.items()):
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    invalid.append(str(key))
                    continue
                if not np.isfinite(numeric) or numeric < 0.0:
                    invalid.append(str(key))
                    continue
                weights[key] = numeric
            invalid.sort()
            if invalid:
                raise ValueError(
                    f"{label} contains negative or non-finite weights: "
                    + ", ".join(invalid)
                )
            if weights and not any(float(value) > 0.0 for value in weights.values()):
                raise ValueError(f"{label} must contain at least one positive weight")
        for pillar, weights in self.metric_weights.items():
            unknown = sorted(set(weights) - set(METRICS.names()))
            misplaced = sorted(
                metric_name
                for metric_name in weights
                if METRICS.maybe(metric_name) is not None
                and METRICS.get(metric_name).pillar != pillar
            )
            if unknown:
                raise ValueError(
                    f"metric_weights[{pillar!r}] contains unknown metrics: "
                    + ", ".join(unknown)
                )
            if misplaced:
                raise ValueError(
                    f"metric_weights[{pillar!r}] contains metrics from another pillar: "
                    + ", ".join(misplaced)
                )
        unknown_metrics = sorted(set(metric_whitelist or ()) - set(METRICS.names()))
        if unknown_metrics:
            raise ValueError(
                "metric_whitelist contains unknown metrics: " + ", ".join(unknown_metrics)
            )
        if normalizer not in NORMALIZERS:
            raise ValueError(f"unknown normalizer {normalizer!r}")
        if normalizer in {"piecewise", "double_sigmoid"}:
            raise ValueError(
                f"{normalizer!r} cannot be used as a global normalizer: piecewise requires "
                "metric-specific calibrated anchors and double_sigmoid requires a metric-specific "
                "ideal band. Direction-0 metrics already use their declared ideal bands."
            )
        for label, method in (
            ("metric_weighting", metric_weighting),
            ("pillar_weighting", pillar_weighting),
        ):
            if method not in WEIGHTINGS:
                raise ValueError(f"unknown {label} method {method!r}")
        for label, method in (
            ("metric_aggregator", metric_aggregator),
            ("pillar_aggregator", pillar_aggregator),
        ):
            if method not in AGGREGATORS:
                raise ValueError(f"unknown {label} method {method!r}")
        parameter_by_aggregator = {
            "ces": {"rho"},
            "trimmed_mean": {"trim"},
            "soft_min": {"temperature"},
            "owa": {"pessimism"},
        }
        allowed_parameters = set().union(
            parameter_by_aggregator.get(metric_aggregator, set()),
            parameter_by_aggregator.get(pillar_aggregator, set()),
        )
        unknown_parameters = sorted(set(self.aggregator_kwargs) - allowed_parameters)
        if unknown_parameters:
            raise ValueError(
                "aggregator_kwargs are not used by the selected aggregators: "
                + ", ".join(unknown_parameters)
            )
        for parameter, value in list(self.aggregator_kwargs.items()):
            try:
                self.aggregator_kwargs[parameter] = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{parameter} must be numeric, got {value!r}") from exc
        if curve not in {"absolute", "cross_sectional", "hybrid"}:
            raise ValueError(
                f"unknown curve {curve!r}; choose absolute, cross_sectional, or hybrid"
            )
        try:
            absolute_weight = float(absolute_weight)
        except (TypeError, ValueError) as exc:
            raise ValueError("absolute_weight must be numeric") from exc
        if not np.isfinite(absolute_weight) or not 0.0 <= absolute_weight <= 1.0:
            raise ValueError("absolute_weight must be finite and between 0 and 1")
        if (
            not isinstance(uncertainty_draws, int)
            or isinstance(uncertainty_draws, bool)
            or uncertainty_draws < 1
        ):
            raise ValueError("uncertainty_draws must be a positive integer")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("seed must be an integer")
        rho = self.aggregator_kwargs.get("rho")
        if rho is not None:
            if not np.isfinite(rho):
                raise ValueError(f"rho must be finite, got {rho!r}")
            if not -20.0 <= float(rho) <= 20.0:
                # Outside this the power mean overflows and the aggregator fails soft to None,
                # which surfaces as an unexplained N/A grade rather than a named bad parameter.
                raise ValueError(
                    f"rho={rho} is outside the usable range [-20, 20]. rho=1 is the arithmetic "
                    f"mean, 0 the geometric, -1 the harmonic; large magnitudes approach min/max "
                    f"and overflow before they get there."
                )
        trim = self.aggregator_kwargs.get("trim")
        if trim is not None and (
            not np.isfinite(trim) or not 0.0 <= float(trim) < 0.5
        ):
            raise ValueError("trim must be finite and in [0, 0.5)")
        temperature = self.aggregator_kwargs.get("temperature")
        if temperature is not None and (
            not np.isfinite(temperature) or float(temperature) < 0.0
        ):
            raise ValueError("temperature must be finite and non-negative")
        pessimism = self.aggregator_kwargs.get("pessimism")
        if pessimism is not None and (
            not np.isfinite(pessimism) or not 0.0 <= float(pessimism) <= 1.0
        ):
            raise ValueError("pessimism must be finite and between 0 and 1")
        self.sector_neutral = sector_neutral
        self.curve = curve
        self.absolute_weight = absolute_weight
        self.seed = seed
        self.uncertainty_draws = uncertainty_draws
        self.gates = gates
        try:
            min_profile_weight_coverage = float(min_profile_weight_coverage)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "min_profile_weight_coverage must be numeric"
            ) from exc
        if (
            not np.isfinite(min_profile_weight_coverage)
            or not 0.0 <= min_profile_weight_coverage <= 1.0
        ):
            raise ValueError(
                "min_profile_weight_coverage must be between 0 and 1, "
                f"got {min_profile_weight_coverage!r}"
        )
        self.min_profile_weight_coverage = float(min_profile_weight_coverage)
        if isinstance(required_pillars, (str, bytes)):
            raise ValueError("required_pillars must be a collection, not a string")
        self.required_pillars = frozenset(
            str(pillar) for pillar in (required_pillars or ())
        )
        unknown_required = sorted(self.required_pillars - valid_pillars)
        if unknown_required:
            raise ValueError(
                "required_pillars contains unknown pillars: " + ", ".join(unknown_required)
            )
        self.require_defining_pillar = bool(require_defining_pillar)
        # This switch exists only for backwards-compatible research experiments.  A bare vector of
        # realised returns is necessarily the same cross-section being graded, so enabling it is
        # an explicit acknowledgement of in-sample look-ahead, never a production default.
        self.allow_in_sample_supervised_weighting = bool(allow_in_sample_supervised_weighting)


def _config_manifest(config: GradeConfig) -> tuple[dict[str, object], str]:
    manifest: dict[str, object] = {
        "profile": config.name,
        "pillar_weights": dict(sorted(config.pillar_weights.items())),
        "metric_weights": {
            pillar: dict(sorted(weights.items()))
            for pillar, weights in sorted(config.metric_weights.items())
        },
        "metric_whitelist": sorted(config.metric_whitelist or ()),
        "normalizer": config.normalizer,
        "metric_weighting": config.metric_weighting,
        "pillar_weighting": config.pillar_weighting,
        "metric_aggregator": config.metric_aggregator,
        "pillar_aggregator": config.pillar_aggregator,
        "aggregator_kwargs": config.aggregator_kwargs,
        "sector_neutral": config.sector_neutral,
        "curve": config.curve,
        "absolute_weight": config.absolute_weight,
        "seed": config.seed,
        "uncertainty_draws": config.uncertainty_draws,
        "gates": config.gates,
        "min_profile_weight_coverage": config.min_profile_weight_coverage,
        "required_pillars": sorted(config.required_pillars),
        "require_defining_pillar": config.require_defining_pillar,
    }
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return manifest, hashlib.sha256(encoded).hexdigest()


def _universe_fingerprint(snapshots: list[SecuritySnapshot]) -> str:
    members = sorted(
        (
            snapshot.ticker.upper(),
            snapshot.cik or "",
            snapshot.asof.isoformat(),
            snapshot.sector.value,
        )
        for snapshot in snapshots
    )
    encoded = json.dumps(members, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _selected_weighting_methods(config: GradeConfig) -> set[str]:
    """Weighting methods that will actually execute under ``config``."""
    # Metric overrides are per pillar, so a non-empty mapping does not prove that every pillar is
    # fixed.  Conservatively include the configured method; false negatives here would reopen the
    # leakage path the guard exists to close.
    methods = {config.metric_weighting}
    methods.add("fixed" if config.pillar_weights else config.pillar_weighting)
    return methods


def _validate_supervised_weighting_safety(config: GradeConfig) -> None:
    """Refuse fitting realised outcomes on the same securities being graded by default."""
    selected = _selected_weighting_methods(config)
    supervised = sorted(
        method
        for method in selected
        if WEIGHT_METHOD_INFO.get(method, {}).get("needs_returns", False)
    )
    if supervised and not config.allow_in_sample_supervised_weighting:
        raise ValueError(
            "in-sample supervised weighting is blocked: "
            f"{', '.join(supervised)} would fit realised forward returns on the same securities "
            "being graded, which is look-ahead leakage. Use a weighting model fitted and frozen "
            "strictly before the grading as-of date. "
            "For backwards-compatible research only, explicitly set "
            "allow_in_sample_supervised_weighting=True."
        )


def _defining_pillars(config: GradeConfig) -> set[str]:
    """Pillars whose absence changes the stated investment style into another strategy."""
    if config.required_pillars:
        return set(config.required_pillars)
    if not config.require_defining_pillar or not config.pillar_weights:
        return set()
    positive = {name: float(weight) for name, weight in config.pillar_weights.items() if weight > 0}
    if not positive:
        return set()
    highest = max(positive.values())
    # Exact ties are deliberate in profiles such as GARP and all-weather; both pillars define the
    # thesis and both must be present.
    return {name for name, weight in positive.items() if abs(weight - highest) <= 1e-12}


def _profile_gate_state(
    live_pillars: set[str],
    pillar_weights: pd.Series,
    config: GradeConfig,
) -> tuple[float, set[str], list[str]]:
    """Nominal profile-weight coverage, required pillars, and hard-gate reasons."""
    # Fixed profiles must be measured against the profile as authored.  ``compute_weights`` first
    # reindexes fixed weights to the pillars present in this run and renormalises them; using that
    # result here would call a profile "100% covered" after an unavailable pillar had simply
    # disappeared from the denominator.  Dynamically weighted configurations have no separate
    # ex-ante vector, so their computed weights are the only meaningful nominal baseline.
    nominal_source = config.pillar_weights if config.pillar_weights else pillar_weights
    nominal = pd.Series(nominal_source, dtype="float64").clip(lower=0.0)
    nominal_total = float(nominal.sum())
    applied = float(nominal.reindex(sorted(live_pillars)).fillna(0.0).sum())
    weight_coverage = applied / nominal_total if nominal_total > 0 else 0.0
    required = _defining_pillars(config)
    missing_required = required - live_pillars
    reasons: list[str] = []
    if config.gates and weight_coverage + 1e-12 < config.min_profile_weight_coverage:
        reasons.append(
            "profile_weight_coverage:"
            f"{weight_coverage:.3f}<{config.min_profile_weight_coverage:.3f}"
        )
    if config.gates and missing_required:
        reasons.append("defining_pillar_missing:" + ",".join(sorted(missing_required)))
    return weight_coverage, required, reasons


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


def _apply_redundancy_groups(weights: pd.Series) -> pd.Series:
    """Metrics in one redundancy group share a single metric's worth of weight.

    P/E and earnings yield are the same signal twice; three FCF multiples are
    the same signal three times. Without this, a signal's effective weight is
    set by how many correlated restatements of it happen to be registered.
    Members are scaled by 1/len(group) and the pillar renormalizes — exactly
    "each group occupies one slot, split equally inside it".
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for name in weights.index:
        spec = METRICS.get(name)
        if spec is not None and spec.group:
            groups[spec.group].append(name)
    multi = {g: m for g, m in groups.items() if len(m) > 1}
    if not multi:
        return weights
    adjusted = weights.astype("float64").copy()
    for members in multi.values():
        adjusted[members] = adjusted[members] / float(len(members))
    total = float(adjusted.sum())
    return adjusted / total if total > 0 else weights


def _pillar_members(names: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for name in names:
        spec = METRICS.maybe(name)
        if spec is not None:
            grouped[spec.pillar].append(name)
    return dict(grouped)


def _sensitivity_range_and_letter_frequencies(
    samples: np.ndarray,
    other_peer_composites: np.ndarray,
    config: GradeConfig,
) -> tuple[tuple[float, float], dict[str, float]]:
    """Model-sensitivity range on the reported scale, plus letter scenario frequencies.

    Mapping only the raw interval endpoints through a fixed point percentile would assign the rank
    component zero width even though each perturbed composite can change rank. Each scenario is
    therefore transformed onto the configured reporting curve before the 5th and 95th scenario
    percentiles are taken. This is an internal model-sensitivity range, not empirical coverage of
    an unknown true grade.

    Each draw now *replaces* the target's point composite among the other peers.  Inserting the draw
    alongside the target's unchanged point would double-count that security, while comparing with
    inconsistent left/right tie rules can move an unchanged tied draw across the entire tie.  The
    shared Hazen midrank operation is identical to point grading.

    The returned frequencies are scenario frequencies under the explicit weight/set perturbations,
    not probabilities of future investment outcomes.
    """
    if samples.size == 0:
        return ((float("nan"), float("nan")), {})
    peers = np.asarray(other_peer_composites, dtype="float64")
    peers = peers[np.isfinite(peers)]
    if config.curve == "absolute":
        reported_samples = samples
        letter_for = to_letter
    elif peers.size < 1:
        # No cross-sectional scale exists.  Preserve a sensitivity range on the raw internal scale
        # for diagnostics, but do not invent letter frequencies from the absolute curve when the
        # configured curve is rank-based.  The report is hard-gated separately.
        reported_samples = samples
        letter_for = None
    else:
        percentiles = np.asarray(
            [hazen_percentile(float(sample), peers) for sample in samples],
            dtype="float64",
        )
        if config.curve == "cross_sectional":
            reported_samples = percentiles
            letter_for = grade_from_percentile
        else:
            weight = config.absolute_weight
            reported_samples = weight * samples + (1.0 - weight) * percentiles
            letter_for = to_letter
    reported_samples = np.clip(reported_samples, 0.0, 100.0)
    low, high = np.percentile(reported_samples, [5.0, 95.0])
    counts: dict[str, int] = {}
    if letter_for is not None:
        for value in reported_samples:
            letter = letter_for(float(value))
            counts[letter] = counts.get(letter, 0) + 1
    frequencies = {
        key: value / len(reported_samples)
        for key, value in sorted(counts.items(), key=lambda item: -item[1])
    }
    return ((float(low), float(high)), frequencies)


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
    _validate_supervised_weighting_safety(config)
    if not snapshots:
        return {}
    config_manifest, config_fingerprint = _config_manifest(config)
    universe_fingerprint = _universe_fingerprint(snapshots)
    universe_members = sorted(snapshot.ticker.upper() for snapshot in snapshots)

    seen: dict[str, int] = {}
    for snapshot in snapshots:
        seen[snapshot.ticker] = seen.get(snapshot.ticker, 0) + 1
    duplicates = sorted(t for t, n in seen.items() if n > 1)
    if duplicates:
        # Results are keyed by ticker, so a repeat overwrites its predecessor and shrinks the peer
        # set. Retain compatibility for now, but make the effective-universe change explicit.
        log.warning(
            "universe contains duplicate tickers (%s); only the last of each is graded and the "
            "effective universe is %d, not %d",
            ", ".join(duplicates[:10]),
            len(seen),
            len(snapshots),
        )

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
    # Time-series diagnostics (Hurst, variance ratio, autocorrelation) carry
    # near-zero cross-sectional pricing content at these universe sizes; they
    # are computed for the evidence dossier but stay OUT of grading unless a
    # config explicitly weights the pillar.
    if "stability" in pillars and "stability" not in (config.pillar_weights or {}):
        del pillars["stability"]
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
        weights = _apply_redundancy_groups(weights)

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
    if config.pillar_weights:
        nominal_pillar_weights = pd.Series(config.pillar_weights, dtype="float64")
        nominal_pillar_weights = nominal_pillar_weights[
            nominal_pillar_weights > 0.0
        ]
        nominal_pillar_weights = nominal_pillar_weights / nominal_pillar_weights.sum()
    else:
        nominal_pillar_weights = pillar_weights.copy()

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

    # Percentiles are computed over GRADEABLE securities only. A name we refuse to grade for lack
    # of data still occupied a rank position, pushing everyone above it up the distribution — worth
    # as much as a letter and a half when several names in a universe are ungradeable. It cannot
    # be a peer for a comparison we decline to make.
    gradeable: dict[str, bool] = {}
    profile_gate_state: dict[str, tuple[float, set[str], list[str]]] = {}
    for snapshot in snapshots:
        results_for = results.get(snapshot.ticker, {})
        ok_count = sum(1 for r in results_for.values() if r.coverage is Coverage.OK)
        missing_count = sum(
            1 for name, r in results_for.items()
            if r.coverage is Coverage.MISSING and name in universe_computable
        )
        applicable_count = ok_count + missing_count
        cov = (ok_count / applicable_count) if applicable_count else 0.0
        live_pillars = {
            pillar
            for pillar, obj in pillar_objects.get(snapshot.ticker, {}).items()
            if np.isfinite(obj.score)
        }
        gate_state = _profile_gate_state(live_pillars, pillar_weights, config)
        profile_gate_state[snapshot.ticker] = gate_state
        _, _, gate_reasons = gate_state
        gradeable[snapshot.ticker] = (
            cov >= MIN_COVERAGE_TO_GRADE and np.isfinite(composite.get(snapshot.ticker, np.nan))
            and not gate_reasons
        )
    ranked_base = composite[[t for t in composite.index if gradeable.get(t)]]
    if len(ranked_base.dropna()) > 1:
        ranked_base = ranked_base.dropna()
        percentiles = pd.Series(
            {
                ticker: hazen_percentile(
                    float(value),
                    ranked_base.drop(index=ticker, errors="ignore").to_numpy(),
                )
                for ticker, value in composite.dropna().items()
            },
            dtype="float64",
        )
    else:
        percentiles = None

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
        metric_errors = sorted(
            name
            for name, result in all_results.items()
            if result.note.startswith("error:")
        )
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
            # Report the percentile, not the raw composite. Under this curve the letter comes from
            # rank, so returning the raw score put the headline number, the letter and the interval
            # on three different scales — a security could show 48.1 next to a D- and an interval
            # ending at 25.
            final_score, letter = float(percentile), grade_from_percentile(percentile)
        else:
            final_score, letter = hybrid_grade(score, percentile, absolute_weight=config.absolute_weight)
        if previous_letters and ticker in previous_letters and np.isfinite(final_score):
            letter = apply_hysteresis(
                float(final_score),
                previous_letters[ticker],
                cutoffs=(
                    PERCENTILE_CUTOFFS
                    if config.curve == "cross_sectional" and percentile is not None
                    else None
                ),
            )

        warns = warnings_by_ticker.get(ticker, [])
        profile_weight_coverage, required_pillars, profile_gate_reasons = profile_gate_state.get(
            ticker,
            _profile_gate_state(set(objects), pillar_weights, config),
        )
        # A pillar that computed fine but carries no weight contributes nothing, silently. That is
        # sometimes deliberate — the momentum profile omits valuation on purpose — but it is also
        # how a newly-working pillar goes unnoticed: risk, momentum and liquidity all computed
        # correctly for months while every profile weighted them at zero, because the profiles were
        # written back when those pillars could never fire.
        unweighted = sorted(
            p for p, obj in objects.items()
            if np.isfinite(obj.score) and nominal_pillar_weights.get(p, 0.0) <= 0.0
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
        if config.curve != "absolute" and percentiles is None:
            warns.insert(
                0,
                "SCORED WITHOUT PEERS: cross-sectional ranks require at least two securities that "
                "pass the data and profile gates. This grade carries no rank information; pass a "
                "broader eligible universe for a meaningful comparison.",
            )
        if run_limited:
            warns.append(
                f"{len(run_limited)} metric(s) unavailable for the entire universe this run "
                f"(e.g. {', '.join(run_limited[:3])}) — excluded from coverage rather than "
                f"charged against every company"
            )
        if snapshot.synthetic_prices:
            warns.insert(0, "price-derived metrics computed from SYNTHETIC prices, not market history")
        if metric_errors:
            warns.append(
                f"{len(metric_errors)} metric implementation error(s) occurred "
                f"({', '.join(metric_errors[:4])}); these are software failures, not missing "
                "company disclosures"
            )

        gates = list(profile_gate_reasons)
        if coverage < MIN_COVERAGE_TO_GRADE:
            gates.append(f"metric_coverage:{coverage:.3f}<{MIN_COVERAGE_TO_GRADE:.3f}")
            warns.append(
                f"refusing to grade: only {coverage:.0%} of applicable metrics could be computed "
                f"(need {MIN_COVERAGE_TO_GRADE:.0%})"
            )
        if not np.isfinite(score):
            gates.append("composite_unavailable")
            warns.append("refusing to grade: no finite composite score could be computed")
        if profile_gate_reasons:
            if profile_weight_coverage + 1e-12 < config.min_profile_weight_coverage:
                warns.append(
                    "refusing to grade: only "
                    f"{profile_weight_coverage:.0%} of the '{config.name}' profile's nominal "
                    "pillar weight could be applied "
                    f"(need {config.min_profile_weight_coverage:.0%})"
                )
            missing_defining = required_pillars - set(objects)
            if missing_defining:
                warns.append(
                    "refusing to grade: defining pillar(s) for this profile did not compute: "
                    + ", ".join(sorted(missing_defining))
                )
        if percentiles is None and config.curve != "absolute":
            # Every cross-sectional score collapsed to the neutral value, so the composite is an
            # artefact of having nothing to compare against. Reporting a letter here would dress
            # up "no information" as a considered C.
            gates.append("gradeable_peer_universe_too_small")
        if gates:
            letter = "N/A"

        # Nominal weights describe the authored profile; effective weights describe THIS grade
        # after missing pillars are removed and the survivors renormalised. Reports retain both so
        # an analyst can see the intended lens and the evidence that actually drove the number.
        live = {p: float(nominal_pillar_weights.get(p, 0.0)) for p in objects}
        live_total = sum(live.values())
        effective_weights = (
            {p: w / live_total for p, w in live.items()} if live_total > 0 else {}
        )
        lost_weight = float(max(0.0, 1.0 - live_total))
        if lost_weight > 0.02:
            warns.append(
                f"{lost_weight:.0%} of this profile's nominal pillar weight could not be applied "
                f"(those pillars did not compute); the grade rests on the remaining "
                f"{1 - lost_weight:.0%}, renormalised"
            )

        pillar_series = pd.Series({p: obj.score for p, obj in objects.items()}, dtype="float64")
        pillar_contributions = explain_aggregate_contributions(
            pillar_series,
            nominal_pillar_weights,
            aggregator=config.pillar_aggregator,
            **config.aggregator_kwargs,
        )
        # Hierarchical attribution: allocate each exact top-level pillar contribution across that
        # pillar's own exact metric contributions. This preserves the two-level model and
        # reconciles to the standardized composite even when either level is nonlinear.
        metric_contributions: dict[str, float] = {}
        for pillar_name, pillar_object in objects.items():
            top_contribution = float(pillar_contributions.get(pillar_name, 0.0))
            within = dict(pillar_object.contributions)
            within_total = float(sum(within.values()))
            if abs(within_total) <= 1e-12:
                # A pillar can land exactly at neutral because positive and negative metrics
                # cancel. Preserve those opposing drivers instead of erasing them; at the
                # all-neutral baseline a CES/power mean's marginal pillar influence equals its
                # effective weight, and the allocations still sum to the zero top contribution.
                local_scale = float(
                    effective_weights.get(
                        pillar_name, nominal_pillar_weights.get(pillar_name, 0.0)
                    )
                )
                for metric_name, within_contribution in within.items():
                    metric_contributions[metric_name] = (
                        local_scale * float(within_contribution)
                    )
                continue
            for metric_name, within_contribution in within.items():
                metric_contributions[metric_name] = (
                    top_contribution * float(within_contribution) / within_total
                )
        curve_effect = (
            float(final_score - score)
            if np.isfinite(final_score) and np.isfinite(score)
            else float("nan")
        )
        attribution_residual = (
            float(final_score - 50.0 - sum(metric_contributions.values()) - curve_effect)
            if np.isfinite(final_score) and np.isfinite(curve_effect)
            else float("nan")
        )
        metric_evidence: dict[str, dict[str, object]] = {}
        for metric_name, metric_result in all_results.items():
            pillar_object = objects.get(metric_result.pillar)
            metric_evidence[metric_name] = {
                "pillar": metric_result.pillar,
                "raw_value": metric_result.value,
                "unit": metric_result.unit,
                "coverage": metric_result.coverage.value,
                "note": metric_result.note,
                "raw_inputs": metric_result.raw_inputs,
                "normalized_score": (
                    pillar_object.metric_scores.get(metric_name)
                    if pillar_object is not None
                    else None
                ),
                "effective_metric_weight": (
                    pillar_object.weights.get(metric_name, 0.0)
                    if pillar_object is not None
                    else 0.0
                ),
                "effective_pillar_weight": effective_weights.get(
                    metric_result.pillar, 0.0
                ),
                "contribution": metric_contributions.get(metric_name, 0.0),
            }
        samples = uncertainty_interval(
            pillar_series,
            nominal_pillar_weights,
            coverage=coverage,
            aggregator=config.pillar_aggregator,
            draws=config.uncertainty_draws,
            seed=config.seed,
            return_samples=True,
            **config.aggregator_kwargs,
        )
        other_peers = (
            ranked_base.drop(index=ticker, errors="ignore").to_numpy()
            if percentiles is not None
            else np.array([], dtype="float64")
        )
        interval, letter_scenario_frequencies = _sensitivity_range_and_letter_frequencies(
            np.asarray(samples), other_peers, config
        )
        if np.isfinite(final_score):
            # The baseline configuration is the scenario the report actually asserts.  A central
            # 5th--95th range can exclude it when a rank jumps at a tie or threshold, so retain the
            # central scenario range while expanding it to include that auditable baseline.
            interval = (
                min(interval[0], float(final_score)),
                max(interval[1], float(final_score)),
            )

        reports[ticker] = GradeReport(
            ticker=ticker,
            asof=snapshot.asof,
            profile=config.name,
            score=final_score,
            letter=letter,
            pillars=objects,
            pillar_weights={k: float(v) for k, v in nominal_pillar_weights.items()},
            effective_pillar_weights=effective_weights,
            lost_weight=lost_weight,
            percentile=percentile,
            ci=interval,
            coverage=coverage,
            weighting_method=f"{config.metric_weighting}/{top_method}",
            normalizer=config.normalizer,
            aggregator=f"{config.metric_aggregator}/{config.pillar_aggregator}",
            gates=gates,
            explain={
                "pillar_contributions": pillar_contributions,
                "metric_contributions": metric_contributions,
                "standardized_composite": float(score),
                "peer_rank_curve_effect": curve_effect,
                "attribution_residual": attribution_residual,
                "n_metrics_ok": ok,
                "n_metrics_missing": missing,
                "n_metrics_not_applicable": sum(
                    1 for r in all_results.values() if r.coverage is Coverage.NOT_APPLICABLE
                ),
                "n_metrics_run_limited": len(run_limited),
                "universe_size": len(snapshots),
                "lost_weight": lost_weight,
                "profile_weight_coverage": profile_weight_coverage,
                "required_pillars": sorted(required_pillars),
                "n_metric_errors": len(metric_errors),
                "metric_errors": metric_errors,
                "metrics": metric_evidence,
                "letter_scenario_frequencies": letter_scenario_frequencies,
                # Backwards-compatible field name.  These are model-scenario frequencies, not
                # probabilities of future returns or intrinsic value.
                "letter_probabilities": letter_scenario_frequencies,
            },
            warnings=warns,
            meta={
                "sector": snapshot.sector.value,
                "industry": snapshot.industry,
                "sic": snapshot.sic,
                "cik": snapshot.cik,
                "company_name": snapshot.name,
                "currency": snapshot.currency,
                "pit_mode": snapshot.meta.get("pit_mode"),
                "curve": config.curve,
                "score_interpretation": {
                    "absolute": "peer_standardized_composite_experimental",
                    "cross_sectional": "peer_percentile",
                    "hybrid": "peer_standardized_and_percentile_blend_experimental",
                }[config.curve],
                "pillar_set": sorted(objects),
                "coverage_penalty": coverage_penalty(coverage),
                "profile_weight_coverage": profile_weight_coverage,
                "interval_kind": "model_sensitivity",
                "schema_version": "1.0",
                "run_status": "graded" if not gates else "refused",
                "config": config_manifest,
                "config_fingerprint": config_fingerprint,
                "universe_fingerprint": universe_fingerprint,
                "universe_members": universe_members,
                "effective_peer_count": max(0, len(ranked_base) - (ticker in ranked_base.index)),
                "price_source": snapshot.meta.get("price_source"),
                "price_date": snapshot.meta.get("price_date"),
                "price_age_days": snapshot.meta.get("price_age_days"),
                "price_lower_bound": snapshot.meta.get("price_lower_bound"),
                "valuation_price_rejected": snapshot.meta.get("valuation_price_rejected"),
                "price_share_basis_check": snapshot.meta.get("price_share_basis_check"),
                "yahoo_share_basis_reconciliation": snapshot.meta.get(
                    "yahoo_share_basis_reconciliation"
                ),
                "price_rejections": snapshot.meta.get("price_rejections", []),
                "price_is_adjusted": bool(snapshot.meta.get("price_is_adjusted", False)),
                "shares_source": snapshot.meta.get("shares_source", "SEC cover page"),
                "shares_date": snapshot.meta.get("shares_date"),
                "benchmark": snapshot.meta.get("benchmark"),
                "benchmark_is_price_only": bool(
                    snapshot.meta.get("benchmark_is_price_only", False)
                ),
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
    would score a flat 50. The default rank-based curve therefore refuses a letter and records that
    the peer universe was absent rather than presenting a neutral artefact as analysis.
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
