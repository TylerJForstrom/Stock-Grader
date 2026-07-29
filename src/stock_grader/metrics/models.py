"""Published forensic-accounting and distress models.

Each is a specific published regression with specific coefficients, so the implementation is
worthless if the coefficients are wrong. They are written out here explicitly with their source, and
each component is exposed in ``raw_inputs`` so a flagged company can be inspected rather than merely
accused.

Beneish and Ohlson need **two consecutive annual periods**; Altman needs one complete annual row.
Each returns ``None`` rather than mixing independently selected periods or emitting a partial score.
"""

from __future__ import annotations

import math

import numpy as np

from ..registry import metric
from ..types import SecuritySnapshot
from .fundamental import _aligned_annual_model_frame
from .util import safe_div

__all__ = ["altman_z_prime", "beneish_m_score", "ohlson_o_score"]


_BENEISH_CONCEPTS = (
    "revenue",
    "receivables",
    "gross_profit",
    "assets",
    "current_assets",
    "ppe_net",
    "depreciation_amortization",
    "sganda_expense",
    "long_term_debt",
    "current_liabilities",
    "net_income",
    "cfo",
)

_OHLSON_CONCEPTS = (
    "assets",
    "liabilities",
    "working_capital",
    "current_liabilities",
    "current_assets",
    "net_income",
    "cfo",
)

_BENEISH_REQUIRED_PERIODS = {"net_income": 1, "cfo": 1}
_OHLSON_REQUIRED_PERIODS = {
    concept: (2 if concept == "net_income" else 1) for concept in _OHLSON_CONCEPTS
}


# Beneish's indices are year-over-year ratios of ratios, so a real company sits near 1.0 — in the
# original paper the manipulator mean for DSRI was 1.46 against 1.03 for non-manipulators. Anything
# far outside that is a tagging artifact rather than a business event, and the model was estimated
# on nothing like it. Lowe's produced a DSRI of 11.63 when its receivables tag changed, which
# carried M to +7.65 and flagged one of the largest retailers in the US as an earnings manipulator.
_INDEX_PLAUSIBLE = (0.2, 5.0)


def _index(current: float | None, prior: float | None) -> float | None:
    """A Beneish-style index: current over prior, refused outside the model's estimation range."""
    value = safe_div(current, prior, positive_denominator=True, cap=20.0)
    if value is None:
        return None
    low, high = _INDEX_PLAUSIBLE
    return value if low <= value <= high else None


@metric("beneish_m_score", pillar="quality", direction=-1, unit="score", winsor=(-8.0, 4.0),
        description="Beneish M-score: likelihood that earnings have been manipulated")
def beneish_m_score(s: SecuritySnapshot) -> tuple[float, dict] | None:
    """Beneish (1999) eight-variable earnings-manipulation model.

    ``M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI
          - 0.172 SGAI + 4.679 TATA - 0.327 LVGI``

    Above **-1.78** flags a company as a likely manipulator. The largest coefficient by far is on
    TATA (total accruals to total assets), which is the same accrual signal Sloan documented —
    earnings arriving as accounting estimates rather than cash.

    Direction is -1: a higher M-score is worse. All eight published indices must resolve from the
    same consecutive fiscal-year pair; otherwise the model returns ``None``.
    """
    frame = _aligned_annual_model_frame(
        s,
        _BENEISH_CONCEPTS,
        periods=2,
        required_periods=_BENEISH_REQUIRED_PERIODS,
    )
    if frame is None:
        return None
    prior = frame.iloc[0]
    current = frame.iloc[1]
    prior_rev, curr_rev = float(prior["revenue"]), float(current["revenue"])
    prior_rec, curr_rec = float(prior["receivables"]), float(current["receivables"])
    prior_gp, curr_gp = float(prior["gross_profit"]), float(current["gross_profit"])
    prior_ta, curr_ta = float(prior["assets"]), float(current["assets"])
    prior_ca, curr_ca = float(prior["current_assets"]), float(current["current_assets"])
    prior_ppe, curr_ppe = float(prior["ppe_net"]), float(current["ppe_net"])
    prior_dep = float(prior["depreciation_amortization"])
    curr_dep = float(current["depreciation_amortization"])
    prior_sga, curr_sga = float(prior["sganda_expense"]), float(current["sganda_expense"])
    prior_ltd = float(prior["long_term_debt"])
    curr_ltd = float(current["long_term_debt"])
    prior_cl = float(prior["current_liabilities"])
    curr_cl = float(current["current_liabilities"])
    curr_ni = float(current["net_income"])
    curr_cfo = float(current["cfo"])

    components: dict[str, object] = {}

    # Days sales in receivables index — receivables growing faster than sales.
    dsri = _index(
        safe_div(curr_rec, curr_rev, positive_denominator=True),
        safe_div(prior_rec, prior_rev, positive_denominator=True),
    )
    # Gross margin index — note the inversion: deteriorating margin gives GMI > 1.
    gmi = _index(
        safe_div(prior_gp, prior_rev, positive_denominator=True),
        safe_div(curr_gp, curr_rev, positive_denominator=True),
    )
    # Asset quality index — the share of assets that is neither current nor plant.
    aqi = None
    if (curr_ca is not None and curr_ppe is not None
            and prior_ca is not None and prior_ppe is not None):
        curr_hard = safe_div(curr_ca + curr_ppe, curr_ta, positive_denominator=True)
        prior_hard = safe_div(prior_ca + prior_ppe, prior_ta, positive_denominator=True)
        if curr_hard is not None and prior_hard is not None:
            aqi = _index(1.0 - curr_hard, 1.0 - prior_hard)
    # Sales growth index.
    sgi = _index(curr_rev, prior_rev)
    # Depreciation index — a falling depreciation rate inflates earnings.
    depi = None
    if (curr_dep is not None and curr_ppe is not None
            and prior_dep is not None and prior_ppe is not None):
        curr_rate = safe_div(curr_dep, curr_dep + curr_ppe, positive_denominator=True)
        prior_rate = safe_div(prior_dep, prior_dep + prior_ppe, positive_denominator=True)
        depi = _index(prior_rate, curr_rate)
    # SG&A index.
    sgai = _index(
        safe_div(curr_sga, curr_rev, positive_denominator=True),
        safe_div(prior_sga, prior_rev, positive_denominator=True),
    )
    # Leverage index.
    lvgi = None
    if (curr_ltd is not None and curr_cl is not None
            and prior_ltd is not None and prior_cl is not None):
        lvgi = _index(
            safe_div(curr_ltd + curr_cl, curr_ta, positive_denominator=True),
            safe_div(prior_ltd + prior_cl, prior_ta, positive_denominator=True),
        )
    # Total accruals to total assets — by far the heaviest-weighted term at +4.679.
    #
    # Gated on positive earnings for exactly the reason accruals_ratio is: a large net loss drives
    # NI - CFO sharply negative, and a positive coefficient on that reads a writedown as pristine
    # earnings quality. Measured TATA: -0.117 for Bed Bath & Beyond and -0.119 for Tupperware, both
    # months from Chapter 11, against -0.002 for Home Depot and -0.047 for Abbott. The two most
    # distressed companies scored best on the model's most influential term.
    tata = None
    if curr_ni is not None and curr_cfo is not None and curr_ni > 0:
        tata = safe_div(curr_ni - curr_cfo, curr_ta, positive_denominator=True)

    terms = {
        "DSRI": (dsri, 0.920),
        "GMI": (gmi, 0.528),
        "AQI": (aqi, 0.404),
        "SGI": (sgi, 0.892),
        "DEPI": (depi, 0.115),
        "SGAI": (sgai, -0.172),
        "TATA": (tata, 4.679),
        "LVGI": (lvgi, -0.327),
    }
    # These are eight terms of one published regression, not optional warning badges. Neutral
    # substitution was especially dangerous for DSRI, GMI, AQI, and LVGI: an extreme input was
    # first quarantined by ``_index`` and then quietly reintroduced as the reassuring value 1.0.
    # If any term is missing or outside the estimation range, there is no Beneish score.
    if any(value is None for value, _ in terms.values()):
        return None

    score = -4.84
    for name, (value, coefficient) in terms.items():
        assert value is not None
        components[name] = float(value)
        score += coefficient * value

    components["flagged"] = float(score > -1.78)
    components["n_components"] = float(len(terms))
    components["n_substituted"] = 0.0
    components["prior_fiscal_period"] = frame.index[0].date().isoformat()
    components["current_fiscal_period"] = frame.index[1].date().isoformat()
    return (float(score), components)


@metric("ohlson_o_score", pillar="health", direction=-1, unit="score", winsor=(-15.0, 15.0),
        description="Ohlson O-score: logit model of bankruptcy probability within two years")
def ohlson_o_score(s: SecuritySnapshot) -> tuple[float, dict] | None:
    """Ohlson (1980) nine-variable bankruptcy logit.

    ``O = -1.32 - 0.407 log(TA/index) + 6.03 TL/TA - 1.43 WC/TA + 0.0757 CL/CA
          - 1.72 OENEG - 2.37 NI/TA - 1.83 FFO/TL + 0.285 INTWO - 0.521 CHIN``

    Higher is worse; ``P(bankruptcy) = 1/(1+exp(-O))``, and O above roughly 0.38 corresponds to a
    greater-than-even probability. Unlike Altman's Z it is a genuine logit rather than a
    discriminant score, and it does not require a market value, so it works for companies whose
    price is unknown.

    The original scales total assets by a GNP price-level index; a constant is used here, which
    shifts every company's score by the same amount and so leaves the cross-sectional ranking — the
    only thing this grader uses — unchanged. The absolute probability is therefore approximate and
    the shift is recorded in ``raw_inputs``.
    """
    frame = _aligned_annual_model_frame(
        s,
        _OHLSON_CONCEPTS,
        periods=2,
        required_periods=_OHLSON_REQUIRED_PERIODS,
    )
    if frame is None:
        return None
    prior = frame.iloc[0]
    current = frame.iloc[1]
    prior_ni = float(prior["net_income"])
    curr_ni = float(current["net_income"])
    total_assets = float(current["assets"])
    total_liabilities = float(current["liabilities"])
    working_capital = float(current["working_capital"])
    current_liabilities = float(current["current_liabilities"])
    current_assets = float(current["current_assets"])
    cfo = float(current["cfo"])

    if total_assets <= 0 or total_liabilities <= 0 or current_assets <= 0:
        return None

    GNP_INDEX = 100.0  # constant stand-in; shifts all scores equally, ranking is unaffected

    size = -0.407 * math.log(total_assets / GNP_INDEX)
    tlta = 6.03 * (total_liabilities / total_assets)
    wcta = -1.43 * (working_capital / total_assets)
    clca = 0.0757 * (current_liabilities / current_assets)
    oeneg = -1.72 * (1.0 if total_liabilities > total_assets else 0.0)
    nita = -2.37 * (curr_ni / total_assets)
    # ``Funds provided by operations`` is represented by the project's canonical CFO concept,
    # matching the metric contract and avoiding an undocumented pretax-income + D&A proxy.
    futl = -1.83 * (cfo / total_liabilities)
    intwo = 0.285 * (1.0 if (curr_ni < 0 and prior_ni < 0) else 0.0)
    denominator = abs(curr_ni) + abs(prior_ni)
    if denominator <= 0:
        return None
    chin = -0.521 * ((curr_ni - prior_ni) / denominator)

    score = -1.32 + size + tlta + wcta + clca + oeneg + nita + futl + intwo + chin
    probability = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, score))))
    return (
        float(score),
        {
            "size": size, "TLTA": tlta, "WCTA": wcta, "CLCA": clca, "OENEG": oeneg,
            "NITA": nita, "FUTL": futl, "INTWO": intwo, "CHIN": chin,
            "bankruptcy_probability": probability,
            "gnp_index_constant": GNP_INDEX,
            "funds_from_operations_proxy": "cash_from_operations",
            "prior_fiscal_period": frame.index[0].date().isoformat(),
            "current_fiscal_period": frame.index[1].date().isoformat(),
        },
    )


@metric("altman_z_prime", group="altman", pillar="health", direction=1, unit="score", winsor=(-10.0, 20.0),
        description="Altman Z'' for non-manufacturers — no market value or sales-to-assets term")
def altman_z_prime(s: SecuritySnapshot) -> tuple[float, dict[str, float]] | None:
    """Altman Z'' (1993), the four-variable revision for non-manufacturers.

    ``Z'' = 6.56 WC/TA + 3.26 RE/TA + 6.72 EBIT/TA + 1.05 BV/TL``

    Below 1.1 is distress, above 2.6 is safe. The sales-to-assets term is dropped precisely because
    it varies enormously across non-manufacturing industries, and book equity replaces market value
    so the score is computable without a price. This is the variant the sector matrix selects for
    service and retail companies; it remains disabled for financials, where none of Altman's models
    were estimated.
    """
    from .fundamental import _altman_model_for

    if _altman_model_for(s) != "z_prime":
        return None
    frame = _aligned_annual_model_frame(
        s,
        (
            "assets",
            "liabilities",
            "working_capital",
            "retained_earnings",
            "equity",
            "ebit",
        ),
        periods=1,
    )
    if frame is None:
        return None
    current = frame.iloc[-1]
    total_assets = float(current["assets"])
    total_liabilities = float(current["liabilities"])
    working_capital = float(current["working_capital"])
    retained = float(current["retained_earnings"])
    equity = float(current["equity"])
    ebit = float(current["ebit"])
    if total_assets <= 0 or total_liabilities <= 0:
        return None

    # Retained earnings over assets is unbounded above, and under US GAAP treasury stock is
    # contra-equity — so a company that has bought back stock for decades carries an enormous
    # retained-earnings balance while its book equity goes negative. Altman's estimation sample
    # contained no such firms.
    #
    # Measured: Bed Bath & Beyond ten months before Chapter 11 had RE/TA of 1.881, book equity of
    # -$220M and TTM EBIT of -$675M, and scored Z'' = 5.21 — deep inside the "safe" zone above
    # 2.6. The 3.26 x RE/TA term alone contributed 6.13, swamping the negative earnings.
    #
    # Winsorising RE/TA is mitigation, not a cure — Texas Instruments carries RE/TA of 1.862 while
    # perfectly healthy, so the ratio being large is not itself a distress signal. The confidence
    # flag does most of the work, and the report demotes Z'' below Ohlson, which handles these
    # firms correctly without special-casing.
    re_ta = float(np.clip(retained / total_assets, -1.0, 1.0))
    score = float(
        6.56 * (working_capital / total_assets)
        + 3.26 * re_ta
        + 6.72 * (ebit / total_assets)
        + 1.05 * (equity / total_liabilities)
    )
    low_confidence = bool(equity < 0 or abs(retained / total_assets) > 1.0)
    return (
        score,
        {
            "WC_TA": working_capital / total_assets,
            "RE_TA_raw": retained / total_assets,
            "RE_TA_used": re_ta,
            "EBIT_TA": ebit / total_assets,
            "BV_TL": equity / total_liabilities,
            "low_confidence": float(low_confidence),
            "negative_equity": float(equity < 0),
        },
    )
