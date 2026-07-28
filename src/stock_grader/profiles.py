"""Grader profiles: complete investment styles, each a full configuration of the pipeline.

A profile answers "graded *as what*?" — the same company is a poor value stock and an excellent
quality one, and a single blended number hides that. Each profile fixes the pillar weights, the
aggregator, and how much a strength may compensate for a weakness, with a stated thesis for why.

The ``rho`` in each profile is the compensation dial from :mod:`stock_grader.aggregate`. Low or
negative ``rho`` means weak pillars drag the grade down hard — appropriate for a deep-value screen,
where the entire risk is that the cheap thing is cheap for a reason. Higher ``rho`` lets genuine
strengths offset ordinary weaknesses, which is right for a quality-compounder mandate.

:func:`consensus_grade` then combines profiles, and reports their **disagreement** as a first-class
output. A stock that grades A on value and F on momentum is a fundamentally different proposition
from one grading C on both, and averaging them to the same C would destroy the only interesting
information in the comparison.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .pipeline import GradeConfig, grade_universe
from .scoring import grade_from_percentile, to_letter
from .types import GradeReport, SecuritySnapshot

__all__ = ["PROFILE_SPECS", "ConsensusResult", "consensus_grade", "get_profile", "profile_names"]


# pillar weights, aggregator rho, thesis
PROFILE_SPECS: dict[str, dict] = {
    "all_weather": {
        "weights": {"valuation": 0.17, "profitability": 0.17, "health": 0.15, "growth": 0.13,
                    "quality": 0.13, "momentum": 0.07, "risk": 0.07, "shareholder": 0.06,
                    "efficiency": 0.04, "liquidity": 0.01},
        "rho": 0.5,
        "thesis": "Balanced default. No single lens dominates, and partial compensation (rho=0.5) "
                  "means a company must be broadly sound rather than spectacular in one dimension.",
    },
    "value": {
        "weights": {"valuation": 0.38, "health": 0.17, "quality": 0.13, "profitability": 0.11,
                    "shareholder": 0.07, "risk": 0.06, "growth": 0.04, "efficiency": 0.04},
        "rho": 0.0,
        "thesis": "Price paid dominates, but solvency and earnings quality carry real weight "
                  "because the failure mode of value investing is the value trap. rho=0 "
                  "(geometric) means a cheap company with a broken balance sheet cannot score well.",
    },
    "deep_value": {
        "weights": {"valuation": 0.52, "health": 0.24, "quality": 0.14, "risk": 0.05,
                    "profitability": 0.05},
        "rho": -0.5,
        "thesis": "Net-net and asset-based bargains. Valuation is most of the grade, but rho=-0.5 "
                  "makes solvency close to a veto: the whole strategy depends on surviving to "
                  "realise the discount.",
    },
    "growth": {
        "weights": {"growth": 0.36, "profitability": 0.19, "momentum": 0.15, "quality": 0.11,
                    "efficiency": 0.07, "health": 0.06, "risk": 0.06},
        "rho": 0.8,
        "thesis": "Compounding revenue and earnings, with quality as a check on growth bought "
                  "through dilution or leverage. High rho — a genuine grower is allowed to look "
                  "expensive.",
    },
    "garp": {
        "weights": {"growth": 0.25, "valuation": 0.25, "profitability": 0.17, "quality": 0.13,
                    "health": 0.10, "momentum": 0.05, "risk": 0.05},
        "rho": 0.3,
        "thesis": "Growth at a reasonable price. Valuation and growth weighted equally, with low "
                  "rho so a company cannot buy an A on one by failing the other.",
    },
    "quality": {
        "weights": {"profitability": 0.28, "quality": 0.22, "health": 0.17, "growth": 0.11,
                    "efficiency": 0.09, "risk": 0.07, "valuation": 0.06},
        "rho": 0.6,
        "thesis": "Durable economic moats: high and stable returns on capital, clean accruals, "
                  "modest leverage. Valuation is deliberately a minor term — this profile answers "
                  "'is this a great business', not 'is it cheap'.",
    },
    "momentum": {
        "weights": {"momentum": 0.50, "risk": 0.20, "growth": 0.14, "profitability": 0.10,
                    "liquidity": 0.06},
        "rho": 0.9,
        "thesis": "Price and earnings trend, risk-adjusted. Liquidity matters because a momentum "
                  "signal you cannot trade is not a signal.",
    },
    "low_volatility": {
        "weights": {"risk": 0.40, "quality": 0.19, "health": 0.17, "profitability": 0.11,
                    "shareholder": 0.08, "liquidity": 0.05},
        "rho": 0.2,
        "thesis": "The low-volatility anomaly: stable, boring, well-capitalised businesses. Low "
                  "rho because the entire premise is the absence of weak spots.",
    },
    "dividend_income": {
        "weights": {"shareholder": 0.34, "health": 0.21, "quality": 0.15, "profitability": 0.13,
                    "valuation": 0.11, "risk": 0.06},
        "rho": 0.1,
        "thesis": "Current income that survives. Payout ratios are scored through an ideal band, "
                  "not maximised — the highest yield in a universe is usually the one about to be "
                  "cut. Very low rho: a stretched balance sheet vetoes the income case.",
    },
    "dividend_growth": {
        "weights": {"shareholder": 0.25, "growth": 0.21, "quality": 0.19, "health": 0.17,
                    "profitability": 0.13, "risk": 0.05},
        "rho": 0.3,
        "thesis": "Rising payouts rather than high ones: moderate current yield, strong coverage, "
                  "and the earnings growth to fund future increases.",
    },
    "turnaround": {
        "weights": {"valuation": 0.30, "momentum": 0.21, "health": 0.19, "growth": 0.15,
                    "quality": 0.09, "risk": 0.06},
        "rho": 0.7,
        "thesis": "Distressed situations where improvement is the thesis. Momentum matters as "
                  "confirmation that the market is starting to agree; high rho tolerates the weak "
                  "trailing fundamentals that define the category.",
    },
}


def profile_names() -> list[str]:
    return sorted(PROFILE_SPECS)


def get_profile(name: str, **overrides) -> GradeConfig:
    """Build a :class:`GradeConfig` from a named profile.

    Any pipeline setting can be overridden, which is how a user asks for "the value profile, but
    weighted by entropy and scored sector-neutral".
    """
    if name not in PROFILE_SPECS:
        raise KeyError(f"unknown profile {name!r}; available: {', '.join(profile_names())}")
    spec = PROFILE_SPECS[name]
    settings = {
        "name": name,
        "pillar_weights": dict(spec["weights"]),
        "pillar_aggregator": "ces",
        "aggregator_kwargs": {"rho": spec["rho"]},
        "pillar_weighting": "fixed",
        "metric_weighting": "equal",
        "metric_aggregator": "weighted_mean",
        "normalizer": "robust_z",
        # Default to an explicitly peer-relative percentile. The historical ``hybrid`` curve
        # blended two peer-derived quantities and was incorrectly described as partly absolute.
        # It remains available as an experimental compatibility option, but is no longer the
        # production default.
        "curve": "cross_sectional",
    }
    settings.update(overrides)
    return GradeConfig(**settings)


class ConsensusResult:
    """A stock viewed through every profile at once.

    Attributes:
        per_profile: each profile's report.
        score: the fit-weighted consensus score.
        letter: consensus letter.
        clarity: 0..100, how much the profiles agree. Low clarity is not a defect in the data —
            it means the security genuinely is a different proposition depending on the lens, which
            is the single most useful thing this object reports.
    """

    def __init__(self, ticker: str, per_profile: dict[str, GradeReport]) -> None:
        self.ticker = ticker
        self.per_profile = per_profile
        scores = pd.Series(
            {
                name: report.score
                for name, report in per_profile.items()
                if report.graded and np.isfinite(report.score)
            },
            dtype="float64",
        )
        self.scores = scores
        if scores.empty:
            self.score, self.letter, self.clarity, self.spread = float("nan"), "N/A", 0.0, float("nan")
            self.best_profile = self.worst_profile = None
            return
        self.score = float(scores.mean())
        self.spread = float(scores.max() - scores.min())
        # 40 points of spread across profiles is total disagreement; 0 is unanimity.
        self.clarity = float(np.clip(100.0 - self.spread * 2.5, 0.0, 100.0))
        # Keep the consensus number and letter on the same scale. Cross-sectional reports use the
        # percentile thresholds; absolute/hybrid reports use the composite thresholds. Older
        # callers may omit curve metadata, in which case a real included report is the safest
        # compatibility fallback.
        median_score = float(scores.median())
        representative = min(
            scores.index,
            key=lambda name: (abs(float(scores[name]) - median_score), str(name)),
        )
        curves = {
            str(per_profile[str(name)].meta.get("curve"))
            for name in scores.index
            if per_profile[str(name)].meta.get("curve")
        }
        if curves == {"cross_sectional"}:
            self.letter = grade_from_percentile(self.score)
        elif curves and curves <= {"absolute", "hybrid"}:
            self.letter = to_letter(self.score)
        else:
            self.letter = per_profile[str(representative)].letter
        self.best_profile = str(scores.idxmax())
        self.worst_profile = str(scores.idxmin())

    def summary(self) -> str:
        if self.scores.empty:
            return f"{self.ticker}: not gradeable"
        return (
            f"{self.ticker}: {self.letter} ({self.score:.1f}) — "
            f"best as {self.best_profile} ({self.scores.max():.0f}), "
            f"worst as {self.worst_profile} ({self.scores.min():.0f}), "
            f"clarity {self.clarity:.0f}/100"
        )

    def to_dict(self) -> dict:
        """Machine-readable consensus, including profiles excluded as ungradeable."""
        return {
            "ticker": self.ticker,
            "score": self.score,
            "letter": self.letter,
            "clarity": self.clarity,
            "spread": self.spread,
            "best_profile": self.best_profile,
            "worst_profile": self.worst_profile,
            "scores": {str(name): float(score) for name, score in self.scores.items()},
            "per_profile": self.per_profile,
        }


def consensus_grade(
    snapshots: list[SecuritySnapshot],
    *,
    profiles: list[str] | None = None,
    **overrides,
) -> dict[str, ConsensusResult]:
    """Grade a universe under every profile and combine.

    The disagreement between profiles is retained rather than averaged away — see
    :class:`ConsensusResult.clarity`.
    """
    names = profiles or profile_names()
    by_profile: dict[str, dict[str, GradeReport]] = {}
    for name in names:
        try:
            config = get_profile(name, **overrides)
        except KeyError:
            continue
        by_profile[name] = grade_universe(snapshots, config)

    results: dict[str, ConsensusResult] = {}
    for snapshot in snapshots:
        ticker = snapshot.ticker
        per_profile = {
            name: reports[ticker] for name, reports in by_profile.items() if ticker in reports
        }
        results[ticker] = ConsensusResult(ticker, per_profile)
    return results
