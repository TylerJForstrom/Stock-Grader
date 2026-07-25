"""Canonical financial concepts and their XBRL tag fallback chains.

Measured across 8 diverse filers (AAPL, GE, JNJ, JPM, SO, SPG, WMT, XOM), only 9 of 20 core
concepts resolved to a single universal tag. Revenue alone needed three different tags; long-term
debt needed three. Every concept therefore carries an *ordered* chain — first tag present wins, and
the winning tag is recorded so a grade can be audited back to the exact line item that produced it.

See docs/design/DATA-GROUND-TRUTH.md §5 for the measured coverage table.
"""

from __future__ import annotations

from ..types import PeriodType

__all__ = ["CONCEPTS", "PERIOD_TYPES", "DEI_CONCEPTS", "concept_names"]


# Ordered fallback chains: canonical name -> XBRL tags, most-preferred first.
CONCEPTS: dict[str, tuple[str, ...]] = {
    # ---- Income statement (duration) ----
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenuesNetOfInterestExpense",  # banks
        "InterestAndDividendIncomeOperating",  # banks, last resort
    ),
    "cogs": (
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
        "CostOfServices",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": (
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeInterestExpenseInterestIncomeIncomeTaxesExtraordinaryItemsNoncontrollingInterestsNet",
    ),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ),
    "income_tax": ("IncomeTaxExpenseBenefit",),
    "interest_expense": (
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestExpenseBorrowings",
        "InterestIncomeExpenseNet",
    ),
    "rnd_expense": ("ResearchAndDevelopmentExpense",),
    "sganda_expense": (
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
    ),
    "operating_expenses": ("OperatingExpenses", "CostsAndExpenses"),
    "eps_diluted": ("EarningsPerShareDiluted", "IncomeLossFromContinuingOperationsPerDilutedShare"),
    "eps_basic": ("EarningsPerShareBasic",),
    "shares_diluted": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ),
    "shares_basic": ("WeightedAverageNumberOfSharesOutstandingBasic",),

    # ---- Balance sheet (instant) ----
    "assets": ("Assets",),
    "current_assets": ("AssetsCurrent",),
    "liabilities": ("Liabilities",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks",
    ),
    "short_term_investments": (
        "ShortTermInvestments",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "MarketableSecuritiesCurrent",
    ),
    "inventory": ("InventoryNet",),
    "receivables": (
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsNotesAndLoansReceivableNetCurrent",
    ),
    "payables": (
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
    ),
    "long_term_debt": (
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    ),
    "short_term_debt": (
        "LongTermDebtCurrent",
        "DebtCurrent",
        "ShortTermBorrowings",
        "OtherShortTermBorrowings",
    ),
    "ppe_net": ("PropertyPlantAndEquipmentNet",),
    "goodwill": ("Goodwill",),
    "intangibles": (
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ),
    "retained_earnings": ("RetainedEarningsAccumulatedDeficit",),
    "preferred_equity": (
        "PreferredStockValue",
        "PreferredStockLiquidationPreferenceValue",
    ),
    "minority_interest": ("MinorityInterest",),

    # ---- Cash flow (duration) ----
    "cfo": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "cfi": (
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ),
    "cff": (
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireRealEstate",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    ),
    "depreciation_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ),
    "dividends_paid": (
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDistributionsToAffiliates",
    ),
    "buybacks": (
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ),
    "stock_issued": (
        "ProceedsFromIssuanceOfCommonStock",
        "ProceedsFromIssuanceOrSaleOfEquity",
    ),
    "share_based_comp": ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
}


# Facts with only an `end` date (a stock, not a flow). Everything else is a duration.
_INSTANT: frozenset[str] = frozenset({
    "assets", "current_assets", "liabilities", "current_liabilities", "equity", "cash",
    "short_term_investments", "inventory", "receivables", "payables", "long_term_debt",
    "short_term_debt", "ppe_net", "goodwill", "intangibles", "retained_earnings",
    "preferred_equity", "minority_interest",
})

PERIOD_TYPES: dict[str, PeriodType] = {
    name: (PeriodType.INSTANT if name in _INSTANT else PeriodType.DURATION)
    for name in CONCEPTS
}

# Per-share and share-count facts must never be summed across quarters the way a flow is.
# EPS is handled by summing (it is additive across quarters); share counts are averaged.
AVERAGED_CONCEPTS: frozenset[str] = frozenset({"shares_diluted", "shares_basic"})

# Entity-level facts from the `dei` taxonomy.
DEI_CONCEPTS: dict[str, tuple[str, ...]] = {
    "shares_outstanding": ("EntityCommonStockSharesOutstanding",),
    "public_float": ("EntityPublicFloat",),
}


def concept_names() -> list[str]:
    """All canonical concept names, sorted."""
    return sorted(CONCEPTS)
