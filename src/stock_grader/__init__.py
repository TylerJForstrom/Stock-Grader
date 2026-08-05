"""Stock-Grader: multi-method statistical stock grading.

Importing this package registers the complete catalogue — every metric, normalizer, aggregator and
weighting method. That is deliberate rather than incidental: the registries fill by decorator side
effect, so a consumer who imported only part of the catalogue previously got a *silently truncated*
one and graded against a different metric set than the CLI does, with no error and no way to tell.
A grade is only comparable to another grade computed from the same catalogue.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Order matters only in that every module must be imported; each registers by decorator.
from . import aggregate, normalize, weighting  # noqa: F401
from .backtest import BacktestConfig, BacktestReport, backtest_to_markdown, evaluate_walk_forward
from .costs import CostEstimate, CostInputs, estimate_cost
from .metrics import fundamental, models, sector_specific, statistical  # noqa: F401
from .peers import PeerSelection, explicit_peers, select_peers
from .pipeline import GradeConfig, grade_universe
from .profiles import consensus_grade, get_profile, profile_names
from .registry import (
    AGGREGATORS,
    METRICS,
    NORMALIZERS,
    WEIGHTINGS,
)
from .research import ResearchReport, build_research_report
from .types import Coverage, GradeReport, PitMode, SecuritySnapshot
from .valuation import DCFScenario, ValuationAnalysis, build_valuation_analysis

__all__ = [
    "AGGREGATORS",
    "METRICS",
    "NORMALIZERS",
    "WEIGHTINGS",
    "BacktestConfig",
    "BacktestReport",
    "CostEstimate",
    "CostInputs",
    "Coverage",
    "DCFScenario",
    "GradeConfig",
    "GradeReport",
    "PeerSelection",
    "PitMode",
    "ResearchReport",
    "SecuritySnapshot",
    "ValuationAnalysis",
    "__version__",
    "backtest_to_markdown",
    "build_research_report",
    "build_valuation_analysis",
    "consensus_grade",
    "estimate_cost",
    "evaluate_walk_forward",
    "explicit_peers",
    "get_profile",
    "grade_universe",
    "profile_names",
    "select_peers",
]
