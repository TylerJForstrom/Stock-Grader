"""SIC-code classification and the metric applicability matrix.

Measured motivation (docs/design/DATA-GROUND-TRUTH.md §6): JPMorgan's XBRL filings contain no
``GrossProfit``, ``CostOfGoodsAndServicesSold``, ``OperatingIncomeLoss``, ``InventoryNet``,
``AssetsCurrent`` or ``LiabilitiesCurrent`` tags. Simon Property Group is missing most of the same
set. These are not download failures — a bank does not publish a classified balance sheet, so its
current ratio is not unknown, it is undefined.

Treating "undefined" as "missing" would penalise the data-coverage score of every financial in the
universe and widen its confidence interval for no reason, so the two states are kept distinct
throughout the pipeline.
"""

from __future__ import annotations

from ..types import SectorClass

__all__ = [
    "SECTOR_DISABLED_METRICS",
    "SECTOR_DISABLED_PILLARS",
    "altman_variant_for",
    "classify_sic",
    "is_applicable",
    "sector_label",
]


# SIC ranges are inclusive on both ends. Order matters: first match wins.
_SIC_RANGES: list[tuple[int, int, SectorClass]] = [
    (6020, 6199, SectorClass.BANK),  # depository + non-depository credit institutions
    (6200, 6299, SectorClass.HOLDING),  # security & commodity brokers
    (6300, 6411, SectorClass.INSURANCE),
    (6500, 6599, SectorClass.REIT),
    (6798, 6798, SectorClass.REIT),  # the actual REIT SIC code
    (6700, 6799, SectorClass.HOLDING),
    (6000, 6019, SectorClass.BANK),
    (4900, 4949, SectorClass.UTILITY),
    (1311, 1311, SectorClass.ENERGY),
    (1381, 1389, SectorClass.ENERGY),
    (2911, 2911, SectorClass.ENERGY),
]


def classify_sic(sic: str | int | None) -> SectorClass:
    """Map an SEC SIC code to a business-model class.

    Unknown or unparseable codes fall back to :attr:`SectorClass.GENERAL`, which enables the full
    standard metric set — the right default for an ordinary operating company.
    """
    if sic is None:
        return SectorClass.GENERAL
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return SectorClass.GENERAL
    for low, high, klass in _SIC_RANGES:
        if low <= code <= high:
            return klass
    return SectorClass.GENERAL


# Metrics that are structurally undefined for a sector class. These resolve to
# Coverage.NOT_APPLICABLE, are renormalised out of their pillar, and carry no coverage penalty.
# Metrics that exist only for one business model. Registering them globally and disabling them
# elsewhere keeps the coverage accounting honest: an industrial company is marked NOT_APPLICABLE
# for "efficiency_ratio" rather than MISSING, so it takes no penalty for lacking a bank metric.
BANK_ONLY_METRICS: frozenset[str] = frozenset({"efficiency_ratio", "net_interest_income_to_assets", "fee_income_share", "deposits_to_assets", "loans_to_deposits", "allowance_coverage", "provision_burden", "tangible_common_equity_ratio"})
REIT_ONLY_METRICS: frozenset[str] = frozenset({"funds_from_operations", "price_to_ffo"})


SECTOR_DISABLED_METRICS: dict[SectorClass, frozenset[str]] = {
    SectorClass.BANK: REIT_ONLY_METRICS | frozenset({
        "current_ratio", "quick_ratio", "cash_ratio", "working_capital_to_assets",
        "inventory_turnover", "days_inventory_outstanding", "cash_conversion_cycle",
        "days_payables_outstanding", "gross_margin", "gross_profit_to_assets",
        "asset_turnover", "capex_intensity", "capex_to_depreciation", "fcf_yield",
        "price_to_fcf", "ev_to_ebitda", "ev_to_ebit", "ev_to_sales", "ev_to_fcf",
        "net_debt_to_ebitda", "debt_to_equity", "interest_coverage", "altman_z",
        "free_cash_flow_margin", "fcf_to_debt", "operating_margin", "ebitda_margin",
        "altman_z_prime", "beneish_m_score",
        "acquirers_multiple", "croic", "reinvestment_rate",
    }),
    SectorClass.INSURANCE: BANK_ONLY_METRICS | REIT_ONLY_METRICS | frozenset({
        "current_ratio", "quick_ratio", "cash_ratio", "inventory_turnover",
        "days_inventory_outstanding", "cash_conversion_cycle", "gross_margin",
        "gross_profit_to_assets", "capex_intensity", "altman_z", "ev_to_ebitda",
        "ev_to_ebit", "net_debt_to_ebitda", "interest_coverage", "acquirers_multiple",
        "altman_z_prime",
    }),
    SectorClass.REIT: BANK_ONLY_METRICS | frozenset({
        "current_ratio", "quick_ratio", "cash_ratio", "inventory_turnover",
        "days_inventory_outstanding", "cash_conversion_cycle", "gross_margin",
        "gross_profit_to_assets", "asset_turnover", "altman_z",
        # Depreciation dominates REIT earnings, so EPS-based valuation is actively misleading.
        "pe_trailing", "pe_forward", "peg_ratio", "earnings_yield", "graham_number",
    }),
    SectorClass.HOLDING: BANK_ONLY_METRICS | REIT_ONLY_METRICS | frozenset({
        "current_ratio", "quick_ratio", "inventory_turnover", "gross_margin",
        "asset_turnover", "capex_intensity", "cash_conversion_cycle", "altman_z",
        "days_inventory_outstanding", "days_sales_outstanding",
    }),
    SectorClass.UTILITY: BANK_ONLY_METRICS | REIT_ONLY_METRICS | frozenset({
        # Leverage is structural and rate-regulated, not a distress signal.
        "debt_to_equity", "net_debt_to_ebitda",
        # Altman Z puts healthy regulated utilities in the distress zone as a matter of course:
        # Southern Company scores 0.94 against a 1.81 threshold. The model's working-capital and
        # sales-to-assets terms assume a manufacturer's balance sheet, not a rate-base one, so the
        # reading is a known false positive rather than a finding. Ohlson's O-score, which does not
        # lean on those terms, stays enabled and puts the same company at a 0.4% failure probability.
        "altman_z", "altman_z_prime",
    }),
    SectorClass.ENERGY: BANK_ONLY_METRICS | REIT_ONLY_METRICS | frozenset(),
    SectorClass.GENERAL: BANK_ONLY_METRICS | REIT_ONLY_METRICS | frozenset(),
}


# Whole pillars that carry no meaning for a sector class.
SECTOR_DISABLED_PILLARS: dict[SectorClass, frozenset[str]] = {
    SectorClass.BANK: frozenset(),
    SectorClass.INSURANCE: frozenset(),
    SectorClass.REIT: frozenset(),
    SectorClass.HOLDING: frozenset(),
    SectorClass.UTILITY: frozenset(),
    SectorClass.ENERGY: frozenset(),
    SectorClass.GENERAL: frozenset(),
}


def is_applicable(metric_name: str, pillar: str, sector: SectorClass) -> bool:
    """Whether a metric is a defined quantity for this business model."""
    if pillar in SECTOR_DISABLED_PILLARS.get(sector, frozenset()):
        return False
    return metric_name not in SECTOR_DISABLED_METRICS.get(sector, frozenset())


def altman_variant_for(sector: SectorClass, *, is_public: bool = True) -> str | None:
    """Which Altman Z variant applies, or ``None`` where the model does not apply at all.

    Altman's models were estimated on manufacturers and later on private and non-manufacturing
    firms. None was estimated on a financial, and the ratios that drive it (working capital, sales
    to assets) are not meaningful for one, so financials return ``None`` rather than a number.
    """
    if sector in (SectorClass.BANK, SectorClass.INSURANCE, SectorClass.HOLDING, SectorClass.REIT):
        return None
    if not is_public:
        return "z_prime"
    return "z"


_LABELS = {
    SectorClass.BANK: "Bank / depository",
    SectorClass.INSURANCE: "Insurance",
    SectorClass.REIT: "Real estate / REIT",
    SectorClass.HOLDING: "Holding / investment",
    SectorClass.UTILITY: "Utility",
    SectorClass.ENERGY: "Energy",
    SectorClass.GENERAL: "Industrial / commercial",
}


def sector_label(sector: SectorClass) -> str:
    return _LABELS.get(sector, sector.value)
