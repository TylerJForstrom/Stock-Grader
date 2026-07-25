"""Published forensic-accounting and distress models.

Each is a specific published regression with specific coefficients, so the implementation is
worthless if the coefficients are wrong. They are written out here explicitly with their source, and
each component is exposed in ``raw_inputs`` so a flagged company can be inspected rather than merely
accused.

All three need **two consecutive annual periods**, and return ``None`` rather than a partial score
when only one is available — a manipulation score computed against a missing prior year is not a
conservative estimate, it is a fabricated one.
"""

from __future__ import annotations

import math

import numpy as np

from ..registry import metric
from ..types import SecuritySnapshot
from .util import safe_div

__all__ = ["altman_z_prime", "beneish_m_score", "ohlson_o_score"]


def _pair(s: SecuritySnapshot, concept: str) -> tuple[float | None, float | None]:
    """(prior year, current year) for an annual concept."""
    f = s.fundamentals
    if f is None or f.annual.empty or concept not in f.annual.columns:
        return (None, None)
    series = f.annual[concept].dropna()
    if len(series) < 2:
        return (None, None)
    return (float(series.iloc[-2]), float(series.iloc[-1]))


def _index(current: float | None, prior: float | None) -> float | None:
    """A Beneish-style index: current over prior, guarded."""
    return safe_div(current, prior, positive_denominator=True, cap=20.0)


@metric("beneish_m_score", pillar="quality", direction=-1, unit="score", winsor=(-8.0, 4.0),
        description="Beneish M-score: likelihood that earnings have been manipulated")
def beneish_m_score(s: SecuritySnapshot) -> tuple[float, dict] | None:
    """Beneish (1999) eight-variable earnings-manipulation model.

    ``M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI
          - 0.172 SGAI + 4.679 TATA - 0.327 LVGI``

    Above **-1.78** flags a company as a likely manipulator. The largest coefficient by far is on
    TATA (total accruals to total assets), which is the same accrual signal Sloan documented —
    earnings arriving as accounting estimates rather than cash.

    Direction is -1: a higher M-score is worse. The score is only as good as its inputs, so any
    company missing more than a couple of the eight indices returns ``None``.
    """
    prior_rev, curr_rev = _pair(s, "revenue")
    prior_rec, curr_rec = _pair(s, "receivables")
    prior_gp, curr_gp = _pair(s, "gross_profit")
    prior_ta, curr_ta = _pair(s, "assets")
    prior_ca, curr_ca = _pair(s, "current_assets")
    prior_ppe, curr_ppe = _pair(s, "ppe_net")
    prior_dep, curr_dep = _pair(s, "depreciation_amortization")
    prior_sga, curr_sga = _pair(s, "sganda_expense")
    prior_ltd, curr_ltd = _pair(s, "long_term_debt")
    prior_cl, curr_cl = _pair(s, "current_liabilities")
    _, curr_ni = _pair(s, "net_income")
    _, curr_cfo = _pair(s, "cfo")

    if curr_rev is None or prior_rev is None or curr_ta is None or prior_ta is None:
        return None

    components: dict[str, float] = {}

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
        "DSRI": (dsri, 0.920, 1.0),
        "GMI": (gmi, 0.528, 1.0),
        "AQI": (aqi, 0.404, 1.0),
        "SGI": (sgi, 0.892, 1.0),
        "DEPI": (depi, 0.115, 1.0),
        "SGAI": (sgai, -0.172, 1.0),
        "TATA": (tata, 4.679, 0.0),
        "LVGI": (lvgi, -0.327, 1.0),
    }
    available = sum(1 for value, _, _ in terms.values() if value is not None)
    if available < 6:
        return None

    score = -4.84
    for name, (value, coefficient, neutral) in terms.items():
        used = neutral if value is None else value
        components[name] = float(used)
        score += coefficient * used

    components["flagged"] = float(score > -1.78)
    components["n_components"] = float(available)
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
    f = s.fundamentals
    if f is None:
        return None
    prior_ni, curr_ni = _pair(s, "net_income")
    total_assets = f.latest("assets")
    total_liabilities = f.latest("liabilities")
    working_capital = f.latest("working_capital")
    current_liabilities = f.latest("current_liabilities")
    current_assets = f.latest("current_assets")
    pretax = f.ttm("pretax_income")
    depreciation = f.ttm("depreciation_amortization")

    if total_assets is None or total_liabilities is None or curr_ni is None:
        return None
    if total_assets <= 0:
        return None
    if total_liabilities <= 0:
        return None

    GNP_INDEX = 100.0  # constant stand-in; shifts all scores equally, ranking is unaffected

    size = -0.407 * math.log(total_assets / GNP_INDEX)
    tlta = 6.03 * (total_liabilities / total_assets)
    wcta = -1.43 * ((working_capital or 0.0) / total_assets)
    clca = 0.0
    if current_assets and current_assets > 0 and current_liabilities is not None:
        clca = 0.0757 * (current_liabilities / current_assets)
    oeneg = -1.72 * (1.0 if total_liabilities > total_assets else 0.0)
    nita = -2.37 * (curr_ni / total_assets)
    ffo = (pretax or 0.0) + (depreciation or 0.0)
    futl = -1.83 * (ffo / total_liabilities)
    intwo = 0.285 * (1.0 if (curr_ni < 0 and (prior_ni is not None and prior_ni < 0)) else 0.0)
    chin = 0.0
    if prior_ni is not None:
        denominator = abs(curr_ni) + abs(prior_ni)
        if denominator > 0:
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
        },
    )


@metric("altman_z_prime", pillar="health", direction=1, unit="score", winsor=(-10.0, 20.0),
        description="Altman Z'' for non-manufacturers — no market value or sales-to-assets term")
def altman_z_prime(s: SecuritySnapshot) -> float | None:
    """Altman Z'' (1993), the four-variable revision for non-manufacturers.

    ``Z'' = 6.56 WC/TA + 3.26 RE/TA + 6.72 EBIT/TA + 1.05 BV/TL``

    Below 1.1 is distress, above 2.6 is safe. The sales-to-assets term is dropped precisely because
    it varies enormously across non-manufacturing industries, and book equity replaces market value
    so the score is computable without a price. This is the variant the sector matrix selects for
    service and retail companies; it remains disabled for financials, where none of Altman's models
    were estimated.
    """
    f = s.fundamentals
    if f is None:
        return None
    total_assets = f.latest("assets")
    total_liabilities = f.latest("liabilities")
    if not total_assets or total_assets <= 0 or not total_liabilities or total_liabilities <= 0:
        return None
    working_capital = f.latest("working_capital")
    retained = f.latest("retained_earnings")
    equity = f.latest("equity")
    ebit = f.ttm("ebit")
    if working_capital is None or retained is None or ebit is None or equity is None:
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
