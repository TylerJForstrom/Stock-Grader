"""Canonical financial concepts and their XBRL tag fallback chains.

Measured across 8 diverse filers (AAPL, GE, JNJ, JPM, SO, SPG, WMT, XOM), only 9 of 20 core
concepts resolved to a single universal tag. Revenue alone needed three different tags; long-term
debt needed three. Every concept therefore carries an *ordered* chain — first tag present wins, and
the winning tag is recorded so a grade can be audited back to the exact line item that produced it.

See docs/design/DATA-GROUND-TRUTH.md §5 for the measured coverage table.

The chains were later audited against SEC's bulk financial statement data sets (3.3M facts from
6,300 filings). Two findings shaped what was added:

* Most apparent "gaps" are **structural, not tagging**. Ranking candidate tags by lift surfaced
  ``InterestExpenseDeposits``, ``NoninterestIncome`` and ``InvestmentCompanyExpense…`` — those
  filings are banks and funds that have no COGS or inventory at all, which the sector applicability
  matrix already handles. Restricting the audit to non-financial filers removed the confound.
* Several high-frequency candidates are **flows, not levels**: ``IncreaseDecreaseInAccountsReceivable``
  is a cash-flow adjustment and ``RepaymentsOfNotesPayable`` a repayment, neither of which is a
  balance. Substituting one for a balance-sheet concept would have produced a confident wrong
  number, so they are deliberately excluded.
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
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
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
        # NOT ...BeforeIncomeTaxesDomestic. That is the US-only slice of a geographic breakdown,
        # never a synonym for consolidated pretax income. McDonald's resolved to it and reported
        # FY2024 pretax income of $3.28B against $8.22B of net income — pretax below net income,
        # which is impossible for a taxpayer. ``ebit = pretax + interest`` inherited the error, so
        # EBIT margin, interest coverage, EV/EBIT and EBITDA were all built on a number about a
        # third of the truth.
    ),
    "income_tax": ("IncomeTaxExpenseBenefit",),
    "interest_expense": (
        "InterestExpense",
        # Used by 1,457 operating-company filings that carry none of the other variants — the
        # single largest tag gap measured against SEC's bulk financial statement data sets.
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestExpenseBorrowings",
        "InterestExpenseOperating",
        "InterestIncomeExpenseNet",
    ),
    "rnd_expense": (
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ),
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
        "AccountsPayableTradeCurrent",
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
        "NotesPayableCurrent",
        "ConvertibleNotesPayableCurrent",
        "OtherShortTermBorrowings",
    ),
    "ppe_net": (
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ),
    "goodwill": ("Goodwill",),
    "intangibles": (
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
        "OtherIntangibleAssetsNet",
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


# Sector-specific chain overrides. Some concepts are not merely tagged differently by business
# model — they are *measured differently*, and resolving them by one global preference order ranks
# companies on quantities that are not comparable.
#
# Revenue for financials is the clearest case. Measured across six large banks, the global chain
# resolved three different bases at once:
#
#   AXP  RevenueFromContractWithCustomerExcludingAssessedTax   $43.1B   (fee income only)
#   BAC  Revenues                                             $115.1B   (gross, before interest expense)
#   GS   RevenuesNetOfInterestExpense                          $60.4B   (net revenue)
#
# Those differ by more than a factor of two for the same kind of business, so price_to_sales and
# net_margin were ranking banks against each other on incompatible measurements. Banks are compared
# on **net revenue** in practice, so that basis is preferred for them and gross is the fallback.
SECTOR_CONCEPT_OVERRIDES: dict[str, dict[str, tuple[str, ...]]] = {
    "bank": {
        "revenue": (
            "RevenuesNetOfInterestExpense",
            "Revenues",
            "InterestAndDividendIncomeOperating",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
    },
    "insurance": {
        "revenue": (
            "Revenues",
            "RevenuesNetOfInterestExpense",
            "PremiumsEarnedNet",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
    },
    "holding": {
        "revenue": (
            "RevenuesNetOfInterestExpense",
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
    },
}


def chains_for(sector: str | None) -> dict[str, tuple[str, ...]]:
    """Concept chains with any sector-specific overrides applied."""
    if not sector or sector not in SECTOR_CONCEPT_OVERRIDES:
        return CONCEPTS
    merged = dict(CONCEPTS)
    merged.update(SECTOR_CONCEPT_OVERRIDES[sector])
    return merged


def concept_names() -> list[str]:
    """All canonical concept names, sorted."""
    return sorted(CONCEPTS)
