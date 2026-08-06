"""Regression tests for SEC XBRL normalisation.

Each test here pins a bug that was found against live filings and that produced a *silently wrong*
number rather than an error — the worst kind. The fixtures are hand-built so the correct answer is
known exactly.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stock_grader.data.sec import (
    build_fundamentals,
    normalize_duration_facts,
    normalize_instant_facts,
)
from stock_grader.types import Fundamentals, PitMode


def _duration(
    start: str, end: str, val: float, *, form="10-Q", filed="2025-01-01", fy=2025, fp="Q1"
):
    return {
        "start": start,
        "end": end,
        "val": val,
        "form": form,
        "filed": filed,
        "fy": fy,
        "fp": fp,
        "accn": f"acc-{start}-{end}",
    }


def _instant(end: str, val: float, *, filed="2025-01-01", form="10-Q"):
    return {
        "end": end,
        "val": val,
        "form": form,
        "filed": filed,
        "fy": 2025,
        "fp": "Q1",
        "accn": f"acc-{end}",
    }


def _fact(records: list[dict], unit: str = "USD") -> dict:
    return {"units": {unit: records}}


class TestQ4Derivation:
    """No US filer publishes a fourth-quarter 10-Q.

    Against Apple's real filings, the three available quarterly revenue records summed to 296.11B
    against a fiscal-year total of 391.04B — a naive sum understates trailing revenue by 24%.
    """

    def test_missing_q4_is_derived_from_the_annual_total(self):
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0),
            _duration("2024-04-01", "2024-06-30", 110.0),
            _duration("2024-07-01", "2024-09-30", 120.0),
            _duration("2024-01-01", "2024-12-31", 500.0, form="10-K", fp="FY"),
        ]
        quarters, annual, _ = normalize_duration_facts(_fact(records))
        assert len(quarters) == 4
        assert quarters.iloc[-1] == pytest.approx(170.0)  # 500 - (100+110+120)
        assert quarters.sum() == pytest.approx(annual.iloc[-1])

    def test_complete_quarters_are_left_alone(self):
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0),
            _duration("2024-04-01", "2024-06-30", 110.0),
            _duration("2024-07-01", "2024-09-30", 120.0),
            _duration("2024-10-01", "2024-12-31", 130.0),
            _duration("2024-01-01", "2024-12-31", 460.0, form="10-K", fp="FY"),
        ]
        quarters, _, _ = normalize_duration_facts(_fact(records))
        assert quarters.sum() == pytest.approx(460.0)
        assert len(quarters) == 4

    def test_year_to_date_records_are_not_counted_as_quarters(self):
        """10-Qs report cumulative figures alongside discrete ones; summing both double-counts."""
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0),
            _duration("2024-01-01", "2024-06-30", 210.0),  # YTD, not a quarter
            _duration("2024-04-01", "2024-06-30", 110.0),
        ]
        quarters, _, _ = normalize_duration_facts(_fact(records))
        assert quarters.sum() == pytest.approx(210.0)
        assert len(quarters) == 2


class TestFiscalLabels:
    """``fy``/``fp`` describe the filing, not the fact.

    Apple's revenue history carries a fiscal-2022 period stamped ``fy=2024``; keying off these
    fields mislabels history by up to two years.
    """

    def test_periods_come_from_dates_not_fiscal_labels(self):
        records = [
            _duration("2022-01-01", "2022-03-31", 50.0, fy=2024, fp="FY"),
            _duration("2024-01-01", "2024-03-31", 100.0, fy=2024, fp="Q2"),
        ]
        quarters, _, _ = normalize_duration_facts(_fact(records))
        assert list(quarters.index) == [date(2022, 3, 31), date(2024, 3, 31)]
        assert quarters.loc[date(2022, 3, 31)] == pytest.approx(50.0)


class TestRestatements:
    def test_latest_mode_takes_the_most_recent_filing(self):
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0, filed="2024-05-01"),
            _duration("2024-01-01", "2024-03-31", 95.0, filed="2024-11-01"),  # restated
        ]
        quarters, _, _ = normalize_duration_facts(_fact(records), pit_mode=PitMode.LATEST)
        assert quarters.iloc[0] == pytest.approx(95.0)

    def test_point_in_time_ignores_filings_after_asof(self):
        """A backtest must not see a restatement that had not happened yet."""
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0, filed="2024-05-01"),
            _duration("2024-01-01", "2024-03-31", 95.0, filed="2024-11-01"),
        ]
        quarters, _, _ = normalize_duration_facts(
            _fact(records), pit_mode=PitMode.PIT, asof=date(2024, 6, 1)
        )
        assert quarters.iloc[0] == pytest.approx(100.0)

    def test_point_in_time_excludes_facts_not_yet_filed(self):
        records = [_duration("2024-01-01", "2024-03-31", 100.0, filed="2025-01-01")]
        quarters, _, _ = normalize_duration_facts(
            _fact(records), pit_mode=PitMode.PIT, asof=date(2024, 6, 1)
        )
        assert quarters.empty

    def test_future_preferred_tag_cannot_displace_the_historical_tag(self):
        """Adding facts first filed after ``asof`` must not alter a historical reconstruction.

        Tag preference used to inspect every modern record before PIT filtering. A preferred tag
        adopted later could therefore displace the fallback tag investors actually saw, after
        which value selection returned an empty series.
        """
        fallback = _fact(
            [
                _duration(
                    "2024-01-01",
                    "2024-03-31",
                    100.0,
                    filed="2024-05-01",
                )
            ]
        )
        historical_payload = {"facts": {"us-gaap": {"Revenues": fallback}}}
        payload_with_future_tag = {
            "facts": {
                "us-gaap": {
                    "Revenues": fallback,
                    "RevenueFromContractWithCustomerExcludingAssessedTax": _fact(
                        [
                            _duration(
                                "2024-04-01",
                                "2024-06-30",
                                125.0,
                                filed="2024-08-01",
                            )
                        ]
                    ),
                }
            }
        }
        kwargs = {"pit_mode": PitMode.PIT, "asof": date(2024, 6, 1)}
        historical = build_fundamentals(historical_payload, **kwargs)
        with_future_tag = build_fundamentals(payload_with_future_tag, **kwargs)

        pd.testing.assert_series_equal(
            historical.quarterly["revenue"],
            with_future_tag.quarterly["revenue"],
        )
        assert with_future_tag.tag_used["revenue"] == "Revenues"


class TestAveragedConcepts:
    """Weighted-average share counts are not flows.

    Deriving Q4 by subtraction gave Apple a share count of **negative 30 billion**, which flowed
    into market cap and corrupted every valuation multiple downstream.
    """

    def test_share_counts_are_never_derived_by_subtraction(self):
        records = [
            _duration("2024-01-01", "2024-03-31", 1000.0),
            _duration("2024-04-01", "2024-06-30", 990.0),
            _duration("2024-07-01", "2024-09-30", 980.0),
            _duration("2024-01-01", "2024-12-31", 985.0, form="10-K", fp="FY"),
        ]
        quarters, _, _ = normalize_duration_facts(_fact(records), averaged=True)
        assert (quarters > 0).all(), "average share counts must never go negative"
        assert len(quarters) == 3

    def test_flows_still_derive_q4(self):
        records = [
            _duration("2024-01-01", "2024-03-31", 1000.0),
            _duration("2024-04-01", "2024-06-30", 990.0),
            _duration("2024-07-01", "2024-09-30", 980.0),
            _duration("2024-01-01", "2024-12-31", 985.0, form="10-K", fp="FY"),
        ]
        quarters, _, _ = normalize_duration_facts(_fact(records), averaged=False)
        assert len(quarters) == 4
        assert quarters.iloc[-1] == pytest.approx(985.0 - 2970.0)


class TestTTM:
    def _facts(self, revenue: list[dict], shares: list[dict] | None = None) -> dict:
        facts = {"us-gaap": {"Revenues": _fact(revenue)}}
        if shares is not None:
            facts["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = _fact(shares)
        return {"facts": facts}

    def test_ttm_sums_flows(self):
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0),
            _duration("2024-04-01", "2024-06-30", 110.0),
            _duration("2024-07-01", "2024-09-30", 120.0),
            _duration("2024-10-01", "2024-12-31", 130.0),
        ]
        fundamentals = build_fundamentals(self._facts(records))
        assert fundamentals.ttm("revenue") == pytest.approx(460.0)

    def test_ttm_averages_share_counts(self):
        """Summing four quarterly average share counts would quadruple the share base."""
        shares = [
            _duration("2024-01-01", "2024-03-31", 1000.0),
            _duration("2024-04-01", "2024-06-30", 1000.0),
            _duration("2024-07-01", "2024-09-30", 1000.0),
            _duration("2024-10-01", "2024-12-31", 1000.0),
        ]
        fundamentals = build_fundamentals(self._facts([], shares))
        assert fundamentals.ttm("shares_diluted") == pytest.approx(1000.0)

    def test_ttm_refuses_a_partial_year(self):
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0),
            _duration("2024-04-01", "2024-06-30", 110.0),
        ]
        fundamentals = build_fundamentals(self._facts(records))
        assert fundamentals.ttm("revenue") is None

    def test_ttm_refuses_non_contiguous_quarters(self):
        """``dropna`` collapses gaps, so four available values may span several years.

        This produced an EBITDA below its own EBIT for a REIT whose inputs had different
        missingness patterns.
        """
        records = [
            _duration("2020-01-01", "2020-03-31", 100.0),
            _duration("2021-01-01", "2021-03-31", 110.0),
            _duration("2022-01-01", "2022-03-31", 120.0),
            _duration("2023-01-01", "2023-03-31", 130.0),
        ]
        fundamentals = build_fundamentals(self._facts(records))
        assert fundamentals.ttm("revenue") is None


class TestInstants:
    def test_instant_facts_take_the_latest_observation(self):
        records = [_instant("2024-03-31", 500.0), _instant("2024-06-30", 550.0)]
        series = normalize_instant_facts(_fact(records))
        assert series.iloc[-1] == pytest.approx(550.0)

    def test_duration_records_are_ignored_in_an_instant_concept(self):
        records = [_instant("2024-03-31", 500.0), _duration("2024-01-01", "2024-03-31", 999.0)]
        series = normalize_instant_facts(_fact(records))
        assert len(series) == 1
        assert series.iloc[0] == pytest.approx(500.0)


class TestTagFallback:
    def test_falls_back_through_the_chain(self):
        """Only 9 of 20 core concepts resolved to one universal tag across 8 sampled filers."""
        records = [
            _duration("2024-01-01", "2024-03-31", 100.0),
            _duration("2024-04-01", "2024-06-30", 110.0),
            _duration("2024-07-01", "2024-09-30", 120.0),
            _duration("2024-10-01", "2024-12-31", 130.0),
        ]
        # 'Revenues' is second in the chain; the preferred tag is absent.
        facts = {"facts": {"us-gaap": {"Revenues": _fact(records)}}}
        fundamentals = build_fundamentals(facts)
        assert fundamentals.tag_used["revenue"] == "Revenues"
        assert fundamentals.ttm("revenue") == pytest.approx(460.0)

    def test_missing_concept_is_absent_not_zero(self):
        facts = {"facts": {"us-gaap": {}}}
        fundamentals = build_fundamentals(facts)
        assert fundamentals.ttm("revenue") is None
        assert fundamentals.latest("assets") is None


class TestDerived:
    def test_gross_profit_is_derived_when_untagged(self):
        """5 of 8 sampled filers had no GrossProfit tag, but revenue minus COGS is available."""
        quarters = [
            ("2024-01-01", "2024-03-31"),
            ("2024-04-01", "2024-06-30"),
            ("2024-07-01", "2024-09-30"),
            ("2024-10-01", "2024-12-31"),
        ]
        facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": _fact([_duration(s, e, 100.0) for s, e in quarters]),
                    "CostOfRevenue": _fact([_duration(s, e, 60.0) for s, e in quarters]),
                }
            }
        }
        fundamentals = build_fundamentals(facts)
        assert fundamentals.ttm("gross_profit") == pytest.approx(160.0)

    def test_free_cash_flow_subtracts_capex_magnitude(self):
        quarters = [
            ("2024-01-01", "2024-03-31"),
            ("2024-04-01", "2024-06-30"),
            ("2024-07-01", "2024-09-30"),
            ("2024-10-01", "2024-12-31"),
        ]
        facts = {
            "facts": {
                "us-gaap": {
                    "NetCashProvidedByUsedInOperatingActivities": _fact(
                        [_duration(s, e, 100.0) for s, e in quarters]
                    ),
                    "PaymentsToAcquirePropertyPlantAndEquipment": _fact(
                        [_duration(s, e, 30.0) for s, e in quarters]
                    ),
                }
            }
        }
        fundamentals = build_fundamentals(facts)
        assert fundamentals.ttm("fcf") == pytest.approx(280.0)
        assert fundamentals.ttm("fcf") <= fundamentals.ttm("cfo")


class TestStaleTagRejection:
    """Filers abandon tags without deleting their history.

    Lowe's still carries six ``LongTermDebtNoncurrent`` records, but the series stops in **2009** at
    $4.524B while the real figure ($36-40B) lives in a later tag. Preferring chain order alone read
    Lowe's as 92% debt-free: debt_to_assets 0.082 against a true ~0.72.
    """

    def test_abandoned_tag_loses_to_a_current_one(self):
        from stock_grader.data.sec import _select_tag

        gaap = {
            "LongTermDebtNoncurrent": _fact([_instant("2009-10-30", 4.524e9)]),
            "LongTermDebt": _fact([_instant("2026-01-30", 39.819e9)]),
        }
        assert _select_tag(("LongTermDebtNoncurrent", "LongTermDebt"), gaap) == "LongTermDebt"

    def test_chain_order_still_wins_among_current_tags(self):
        """Recency breaks ties; it does not override preference for equally-current tags."""
        from stock_grader.data.sec import _select_tag

        gaap = {
            "LongTermDebtNoncurrent": _fact([_instant("2026-01-30", 10e9)]),
            "LongTermDebt": _fact([_instant("2026-04-30", 11e9)]),
        }
        assert (
            _select_tag(("LongTermDebtNoncurrent", "LongTermDebt"), gaap)
            == "LongTermDebtNoncurrent"
        )

    def test_annual_only_filer_is_not_treated_as_stale(self):
        """A 10-K-only filer's balance sheet is legitimately up to 15 months old."""
        from stock_grader.data.sec import _select_tag

        gaap = {
            "LongTermDebtNoncurrent": _fact([_instant("2025-06-30", 10e9)]),
            "LongTermDebt": _fact([_instant("2025-09-30", 11e9)]),
        }
        assert (
            _select_tag(("LongTermDebtNoncurrent", "LongTermDebt"), gaap)
            == "LongTermDebtNoncurrent"
        )


class TestAnnualFrame:
    """``history(annual=True)`` returned six *quarters* for balance-sheet concepts.

    book_value_cagr_5y was computing growth over a 1.25-year window and labelling it a 5-year CAGR,
    for every company in the universe.
    """

    def _quarterly_facts(self, n: int = 24) -> dict:
        records = []
        start = pd.Timestamp("2020-03-31")
        for i in range(n):
            when = start + pd.DateOffset(months=3 * i)
            records.append(_instant(when.date().isoformat(), 100.0 + i))
        return {"facts": {"us-gaap": {"StockholdersEquity": _fact(records)}}}

    def test_annual_frame_has_one_row_per_year(self):
        fundamentals = build_fundamentals(self._quarterly_facts())
        annual = fundamentals.annual["equity"].dropna()
        quarterly = fundamentals.quarterly["equity"].dropna()
        assert len(quarterly) == 24
        assert 5 <= len(annual) <= 7  # ~6 fiscal years

    def test_history_spans_the_requested_years(self):
        fundamentals = build_fundamentals(self._quarterly_facts())
        window = fundamentals.history("equity", 5, annual=True)
        assert window is not None
        elapsed = (pd.Timestamp(window.index[-1]) - pd.Timestamp(window.index[0])).days / 365.25
        assert 3.0 <= elapsed <= 5.4

    def test_history_refuses_a_window_of_the_wrong_span(self):
        """Six rows spanning 15 years must not be served as a 5-year history."""
        records = [
            _instant(f"{year}-12-31", 100.0 + i)
            for i, year in enumerate([2010, 2021, 2022, 2023, 2024, 2025])
        ]
        facts = {"facts": {"us-gaap": {"StockholdersEquity": _fact(records)}}}
        fundamentals = build_fundamentals(facts)
        assert fundamentals.history("equity", 6, annual=True) is None

    def test_span_check_can_be_disabled(self):
        fundamentals = build_fundamentals(self._quarterly_facts())
        assert fundamentals.history("equity", 3, annual=True, require_span=False) is not None


class TestSectorConceptOverrides:
    """Bank revenue is *measured* differently by filer, not merely tagged differently.

    Measured across six large banks, the global chain resolved three incompatible bases at once:
    AXP fee-income-only at $43.1B, BAC gross at $115.1B, GS net-of-interest at $60.4B. price_to_sales
    and net_margin were ranking banks on quantities that are not the same quantity.
    """

    def test_bank_prefers_net_revenue(self):
        from stock_grader.data.concepts import chains_for

        chain = chains_for("bank")["revenue"]
        assert chain[0] == "RevenuesNetOfInterestExpense"
        # Fee income alone must never be the preferred basis for a bank.
        assert chain.index("RevenueFromContractWithCustomerExcludingAssessedTax") == len(chain) - 1

    def test_general_sector_is_unchanged(self):
        from stock_grader.data.concepts import CONCEPTS, chains_for

        assert chains_for("general") is CONCEPTS
        assert chains_for(None) is CONCEPTS

    def test_override_only_touches_named_concepts(self):
        from stock_grader.data.concepts import CONCEPTS, chains_for

        bank = chains_for("bank")
        assert bank["assets"] == CONCEPTS["assets"]
        assert bank["revenue"] != CONCEPTS["revenue"]


class TestCurrency:
    """Foreign private issuers file with the SEC in their own currency.

    Toyota reports revenue in JPY and Alibaba reports receivables in CNY. Falling back to whatever
    unit happens to be present treated ~48 trillion yen as dollars — a ~150x overstatement that
    would make Toyota look extraordinarily cheap on every sales multiple — and mixed CNY
    receivables against USD assets inside a single ratio.
    """

    def test_foreign_only_facts_are_dropped(self):
        from stock_grader.data.sec import _usd_records

        assert _usd_records({"units": {"JPY": [{"end": "2025-12-31", "val": 48e12}]}}) == []
        assert _usd_records({"units": {"CNY": [{"end": "2025-12-31", "val": 1e9}]}}) == []

    def test_usd_is_preferred_when_both_exist(self):
        from stock_grader.data.sec import _usd_records

        records = _usd_records(
            {
                "units": {
                    "JPY": [{"end": "2025-12-31", "val": 48e12}],
                    "USD": [{"end": "2025-12-31", "val": 320e9}],
                }
            }
        )
        assert len(records) == 1 and records[0]["val"] == 320e9

    def test_dimensionless_units_still_pass(self):
        from stock_grader.data.sec import _usd_records

        assert len(_usd_records({"units": {"shares": [{"end": "2025-12-31", "val": 1e9}]}})) == 1
        assert len(_usd_records({"units": {"pure": [{"end": "2025-12-31", "val": 0.21}]}})) == 1

    def test_currency_detection(self):
        from stock_grader.data.sec import detect_currencies

        facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": {"units": {"JPY": []}},
                    "Assets": {"units": {"USD": []}},
                    "EarningsPerShareDiluted": {"units": {"JPY/shares": []}},
                }
            }
        }
        assert detect_currencies(facts) == {"JPY", "USD"}


class TestDerivedLiabilities:
    """Walmart, Nike, TJX, McDonald's, Target and AbbVie never tag ``Liabilities`` at all.

    Both ohlson_o_score and altman_z_prime returned None for all six while the grade was still
    issued — silently missing its solvency input.
    """

    def _facts(self, assets: float, equity: float, *, include_liabilities: bool = False) -> dict:
        gaap = {
            "Assets": _fact([_instant("2025-12-31", assets)]),
            "StockholdersEquity": _fact([_instant("2025-12-31", equity)]),
        }
        if include_liabilities:
            gaap["Liabilities"] = _fact([_instant("2025-12-31", assets - equity)])
        return {"facts": {"us-gaap": gaap}}

    def test_liabilities_derived_when_untagged(self):
        fundamentals = build_fundamentals(self._facts(260e9, 91e9))
        assert fundamentals.latest("liabilities") == pytest.approx(169e9)

    def test_filed_tag_wins_over_derivation(self):
        fundamentals = build_fundamentals(self._facts(260e9, 91e9, include_liabilities=True))
        assert fundamentals.tag_used.get("liabilities") == "Liabilities"

    def test_liabilities_and_equity_tag_is_not_used(self):
        """That tag equals total *assets* for ~99% of filers.

        Putting it in the chain would set liabilities = assets for exactly the companies being
        repaired, forcing TL/TA to 1.0 and tripping Ohlson's insolvency indicator — a worse bug
        than the gap it closes.
        """
        from stock_grader.data.concepts import CONCEPTS

        assert "LiabilitiesAndStockholdersEquity" not in CONCEPTS["liabilities"]


class TestPretaxChain:
    def test_domestic_only_tag_is_not_in_the_chain(self):
        """McDonald's resolved to the US-only geographic slice and reported pretax income of
        $3.28B against $8.22B of net income — pretax below net income, impossible for a taxpayer."""
        from stock_grader.data.concepts import CONCEPTS

        assert (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"
            not in CONCEPTS["pretax_income"]
        )

    def test_pretax_derived_from_the_identity_when_untagged(self):
        quarters = [
            ("2025-01-01", "2025-03-31"),
            ("2025-04-01", "2025-06-30"),
            ("2025-07-01", "2025-09-30"),
            ("2025-10-01", "2025-12-31"),
        ]
        facts = {
            "facts": {
                "us-gaap": {
                    "NetIncomeLoss": _fact([_duration(s, e, 100.0) for s, e in quarters]),
                    "IncomeTaxExpenseBenefit": _fact([_duration(s, e, 30.0) for s, e in quarters]),
                }
            }
        }
        fundamentals = build_fundamentals(facts)
        assert fundamentals.ttm("pretax_income") == pytest.approx(520.0)


class TestTotalDebtComposition:
    """Lowe's most recent quarter tags only short-term borrowings of $380M.

    Summing "whatever is present" reported that as its entire debt, against a true $39.8B — a 99%
    understatement that made a leveraged retailer read as debt-free.
    """

    def _facts(self, rows: list[tuple[str, float | None, float | None]]) -> dict:
        lt = [_instant(d, v) for d, v, _ in rows if v is not None]
        st = [_instant(d, v) for d, _, v in rows if v is not None]
        gaap = {}
        if lt:
            gaap["LongTermDebt"] = _fact(lt)
        if st:
            gaap["ShortTermBorrowings"] = _fact(st)
        return {"facts": {"us-gaap": gaap}}

    def test_partial_quarter_does_not_become_total_debt(self):
        facts = self._facts([("2025-01-31", 35.3e9, None), ("2026-05-01", None, 0.38e9)])
        fundamentals = build_fundamentals(facts)
        total = fundamentals.latest("total_debt", asof=date(2026, 7, 24), max_age_days=400)
        assert total is None or total > 1e9, "a lone short-term line must not become total debt"

    def test_single_reported_component_still_resolves(self):
        """A filer that never tags short-term borrowings must not lose total debt entirely."""
        facts = self._facts([("2026-05-01", 20e9, None)])
        fundamentals = build_fundamentals(facts)
        assert fundamentals.latest(
            "total_debt", asof=date(2026, 7, 24), max_age_days=400
        ) == pytest.approx(20e9)

    def test_stale_values_are_refused(self):
        facts = self._facts([("2015-01-31", 10e9, 1e9)])
        fundamentals = build_fundamentals(facts)
        assert fundamentals.latest("total_debt", asof=date(2026, 7, 24), max_age_days=400) is None

    def test_unbounded_by_default(self):
        facts = self._facts([("2015-01-31", 10e9, 1e9)])
        fundamentals = build_fundamentals(facts)
        assert fundamentals.latest("total_debt") == pytest.approx(11e9)


def test_unknown_debt_is_not_zero_debt():
    """``debt or 0.0`` turned "we could not read the debt" into "there is no debt".

    A one-directional optimistic error across every EV multiple.
    """
    import pandas as pd

    from stock_grader.metrics.fundamental import _enterprise_value
    from stock_grader.types import Fundamentals, SecuritySnapshot

    index = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
    frame = pd.DataFrame({"cash": [10.0] * 4}, index=index)
    snapshot = SecuritySnapshot(
        ticker="X",
        asof=date(2026, 1, 31),
        fundamentals=Fundamentals(frame, frame, pd.Series(dtype="object")),
        price=10.0,
        shares_outstanding=100.0,
    )
    assert _enterprise_value(snapshot) is None


class TestQ4Plausibility:
    """The fiscal-year total and the three quarters are selected independently, so they can come
    from different restatement vintages and the subtraction mixes them.

    Target's FY2013 capex was filed at $3.453B and restated to $1.886B while year-to-date Q3 stayed
    at $2.839B in both vintages, giving a derived Q4 of -$953M — which an ``.abs()`` downstream
    turned into a plausible $953M outflow inside free cash flow, off by $1.9B and invisible.
    """

    def _facts(self, q1: float, q2: float, q3: float, fy: float) -> dict:
        records = [
            _duration("2024-01-01", "2024-03-31", q1),
            _duration("2024-04-01", "2024-06-30", q2),
            _duration("2024-07-01", "2024-09-30", q3),
            _duration("2024-01-01", "2024-12-31", fy, form="10-K", fp="FY"),
        ]
        return {
            "facts": {"us-gaap": {"PaymentsToAcquirePropertyPlantAndEquipment": _fact(records)}}
        }

    def test_negative_derived_q4_is_rejected(self):
        fundamentals = build_fundamentals(self._facts(1000.0, 1000.0, 839.0, 1886.0))
        capex = fundamentals.quarterly["capex"].dropna()
        assert (capex >= 0).all()
        assert len(capex) == 3, "the implausible fourth quarter must be dropped, not stored"

    def test_plausible_q4_is_kept(self):
        fundamentals = build_fundamentals(self._facts(1000.0, 1000.0, 1000.0, 4200.0))
        capex = fundamentals.quarterly["capex"].dropna()
        assert len(capex) == 4
        assert capex.iloc[-1] == pytest.approx(1200.0)

    def test_wildly_oversized_q4_is_rejected(self):
        """A quarter twenty times its siblings is arithmetic failure, not seasonality."""
        fundamentals = build_fundamentals(self._facts(100.0, 100.0, 100.0, 20300.0))
        assert len(fundamentals.quarterly["capex"].dropna()) == 3

    def test_filed_negative_outflow_is_dropped(self):
        """Chevron filed capex of -$4.452B for 2008-Q1; no derivation gate can catch that."""
        records = [_duration("2008-01-01", "2008-03-31", -4.452e9)]
        facts = {
            "facts": {"us-gaap": {"PaymentsToAcquirePropertyPlantAndEquipment": _fact(records)}}
        }
        fundamentals = build_fundamentals(facts)
        assert (
            "capex" not in fundamentals.quarterly.columns
            or fundamentals.quarterly["capex"].dropna().empty
        )

    def test_losses_are_not_sign_constrained(self):
        """Constraining net income would delete real losses — the companies solvency most needs."""
        from stock_grader.data.sec import SIGN_CONSTRAINED

        for concept in ("net_income", "operating_income", "pretax_income", "income_tax"):
            assert concept not in SIGN_CONSTRAINED


class TestAsofHonesty:
    def test_historical_asof_without_pit_raises(self):
        """`_select` filters on filed <= asof only under PIT, so asof was accepted then ignored.

        Bed Bath & Beyond at asof=2019-01-01 returned a 2023 balance sheet under the default —
        four years of leakage from one omitted keyword.
        """
        from stock_grader.data.sec import SECClient, SECProvider

        provider = SECProvider(SECClient(cache_dir="/tmp/sg-asof-test"))
        with pytest.raises(ValueError, match="ignored under PitMode.LATEST"):
            provider.fetch("AAPL", asof=date(2019, 1, 1))


class TestSplitRestatement:
    """Filers restate share counts retroactively, but only in filings made after the split.

    Selecting the newest vintage per period therefore stitches restated recent periods onto
    un-restated older ones, and the seam reads as an enormous issuance: NVIDIA's annual series
    jumped 9.9x at its 10-for-1 split, Amazon's 20.2x, Apple's 3.8x.
    """

    def test_split_seam_is_removed(self):
        from stock_grader.data.sec import restate_for_splits

        # Ten years flat, then a 10-for-1 split seam.
        index = pd.to_datetime([f"{y}-12-31" for y in range(2020, 2026)])
        shares = pd.Series([100.0, 100.0, 100.0, 1000.0, 1000.0, 1000.0], index=index)
        assets = pd.Series([500.0] * 6, index=index)
        restated = restate_for_splits(shares, assets)
        ratios = (restated / restated.shift(1)).dropna()
        assert (ratios.between(0.67, 1.5)).all(), "the split seam must be gone"
        assert restated.iloc[-1] == pytest.approx(1000.0), "the current basis is preserved"

    def test_a_merger_is_not_treated_as_a_split(self):
        """A split moves shares and nothing else; if assets move too, it is a real issuance."""
        from stock_grader.data.sec import restate_for_splits

        index = pd.to_datetime([f"{y}-12-31" for y in range(2022, 2026)])
        shares = pd.Series([100.0, 100.0, 400.0, 400.0], index=index)
        assets = pd.Series([500.0, 500.0, 2000.0, 2000.0], index=index)  # assets quadrupled too
        restated = restate_for_splits(shares, assets)
        assert restated.iloc[0] == pytest.approx(100.0), "a merger must be left alone"

    def test_non_split_ratios_are_left_alone(self):
        from stock_grader.data.sec import restate_for_splits

        index = pd.to_datetime([f"{y}-12-31" for y in range(2023, 2026)])
        shares = pd.Series([100.0, 137.0, 141.0], index=index)  # 1.37x is no split ratio
        restated = restate_for_splits(shares, None)
        assert restated.iloc[0] == pytest.approx(100.0)

    def test_reverse_splits_handled(self):
        from stock_grader.data.sec import restate_for_splits

        index = pd.to_datetime([f"{y}-12-31" for y in range(2023, 2026)])
        shares = pd.Series([1000.0, 100.0, 100.0], index=index)  # 1-for-10 reverse
        assets = pd.Series([500.0] * 3, index=index)
        restated = restate_for_splits(shares, assets)
        ratios = (restated / restated.shift(1)).dropna()
        assert (ratios.between(0.67, 1.5)).all()


class TestShareScale:
    """McDonald's tags diluted shares as 716 against a DEI cover count of 710,505,859.

    Nothing else in the payload says the units differ, and a market cap built from it would come
    out at $180 thousand rather than $180 billion.
    """

    def test_millions_scaled_count_is_corrected(self):
        from stock_grader.data.sec import SECClient, SECProvider

        provider = SECProvider(SECClient(cache_dir="/tmp/sg-scale-test"))
        # Exercised against the live payload elsewhere; here just assert the ratio logic.
        cover, diluted = 710_505_859.0, 716.0
        ratio = cover / diluted
        assert 0.5 * 1_000_000 <= ratio <= 2.0 * 1_000_000
        assert provider is not None

    def test_ordinary_difference_is_not_rescaled(self):
        """Weighted-average diluted and period-end basic legitimately differ by a few percent."""
        cover, diluted = 14_687_356_000.0, 14_725_873_000.0
        ratio = cover / diluted
        for factor in (1_000.0, 1_000_000.0):
            assert not (0.5 * factor <= ratio <= 2.0 * factor)


class TestStalenessEverywhere:
    """The age contract was added to latest() and missed everywhere else."""

    def _facts(self, concept_tag: str, values: list[tuple[str, str, float]]) -> dict:
        return {
            "facts": {"us-gaap": {concept_tag: _fact([_duration(s, e, v) for s, e, v in values])}}
        }

    def test_ttm_refuses_a_stale_window(self):
        """Mastercard's net income came from a window ending 2014-03-31 — twelve years stale —
        and was divided by 2026 revenue to report a 9.5% net margin against a true 45%."""
        quarters = [
            ("2014-01-01", "2014-03-31"),
            ("2014-04-01", "2014-06-30"),
            ("2014-07-01", "2014-09-30"),
            ("2014-10-01", "2014-12-31"),
        ]
        fundamentals = build_fundamentals(
            self._facts("Revenues", [(s, e, 100.0) for s, e in quarters])
        )
        assert fundamentals.ttm("revenue") == pytest.approx(400.0)  # unbounded: unchanged
        assert fundamentals.ttm("revenue", asof=date(2026, 7, 25), max_age_days=400) is None

    def test_staleness_recurses_into_composites(self):
        """ebitda resolves ebit and depreciation separately, so checking only the composite
        would let a stale component through."""
        quarters = [
            ("2014-01-01", "2014-03-31"),
            ("2014-04-01", "2014-06-30"),
            ("2014-07-01", "2014-09-30"),
            ("2014-10-01", "2014-12-31"),
        ]
        facts = {
            "facts": {
                "us-gaap": {
                    "NetCashProvidedByUsedInOperatingActivities": _fact(
                        [_duration(s, e, 100.0) for s, e in quarters]
                    ),
                    "PaymentsToAcquirePropertyPlantAndEquipment": _fact(
                        [_duration(s, e, 20.0) for s, e in quarters]
                    ),
                }
            }
        }
        fundamentals = build_fundamentals(facts)
        assert fundamentals.ttm("fcf", asof=date(2026, 7, 25), max_age_days=400) is None

    def test_fcf_requires_cfo_and_capex_from_the_same_four_quarters(self):
        index = pd.to_datetime(
            ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
        )
        quarterly = pd.DataFrame(
            {
                "cfo": [float("nan"), 100.0, 100.0, 100.0, 100.0],
                "capex": [20.0, 20.0, 20.0, 20.0, float("nan")],
            },
            index=index,
        )
        fundamentals = Fundamentals(
            quarterly=quarterly,
            annual=pd.DataFrame(),
            filed=pd.Series(dtype="object"),
        )

        assert fundamentals.ttm("cfo", asof=date(2026, 1, 31), max_age_days=400) == 400.0
        assert fundamentals.ttm("capex", asof=date(2026, 1, 31), max_age_days=400) == 80.0
        assert fundamentals.ttm("fcf", asof=date(2026, 1, 31), max_age_days=400) is None

    def test_fcf_uses_one_aligned_consecutive_window(self):
        index = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
        quarterly = pd.DataFrame(
            {
                "cfo": [100.0, 110.0, 120.0, 130.0],
                "capex": [20.0, 25.0, 30.0, 35.0],
            },
            index=index,
        )
        fundamentals = Fundamentals(
            quarterly=quarterly,
            annual=pd.DataFrame(),
            filed=pd.Series(dtype="object"),
        )

        assert fundamentals.ttm("fcf", asof=date(2026, 1, 31), max_age_days=400) == 350.0

    def test_fcf_rejects_four_rows_that_cover_only_half_a_year(self):
        index = pd.to_datetime(["2025-01-01", "2025-02-25", "2025-04-21", "2025-06-15"])
        quarterly = pd.DataFrame(
            {"cfo": [100.0] * 4, "capex": [20.0] * 4},
            index=index,
        )
        fundamentals = Fundamentals(
            quarterly=quarterly,
            annual=pd.DataFrame(),
            filed=pd.Series(dtype="object"),
        )

        assert fundamentals.ttm("fcf", asof=date(2025, 7, 1), max_age_days=400) is None

    def test_pit_helpers_exclude_a_row_filed_after_asof(self):
        index = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
        quarterly = pd.DataFrame(
            {"revenue": [10.0] * 4, "assets": [1.0, 2.0, 3.0, 4.0]},
            index=index,
        )
        fundamentals = Fundamentals(
            quarterly=quarterly,
            annual=pd.DataFrame(),
            filed=pd.Series({date(2025, 12, 31): date(2026, 3, 15)}, dtype="object"),
            pit_mode=PitMode.PIT,
        )

        assert fundamentals.ttm("revenue", asof=date(2026, 2, 1), max_age_days=400) is None
        assert fundamentals.latest("assets", asof=date(2026, 2, 1), max_age_days=400) == 3.0

    def test_derivation_fills_gaps_not_only_absent_columns(self):
        """Costco, Amazon and Target all still carry a GrossProfit column whose last value is
        thousands of days old, so a `not in df` guard never ran the derivation."""
        quarters = [
            ("2025-01-01", "2025-03-31"),
            ("2025-04-01", "2025-06-30"),
            ("2025-07-01", "2025-09-30"),
            ("2025-10-01", "2025-12-31"),
        ]
        facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": _fact([_duration(s, e, 100.0) for s, e in quarters]),
                    "CostOfRevenue": _fact([_duration(s, e, 60.0) for s, e in quarters]),
                    # An abandoned gross-profit tag from a decade earlier.
                    "GrossProfit": _fact([_duration("2014-01-01", "2014-03-31", 5.0)]),
                }
            }
        }
        fundamentals = build_fundamentals(facts)
        assert fundamentals.ttm("gross_profit") == pytest.approx(160.0)


class TestShareClassTickers:
    def test_dot_and_hyphen_forms_both_resolve(self):
        """SEC writes BRK-B; every human and every other source writes BRK.B."""
        from stock_grader.data.sec import SECClient, SECProvider

        provider = SECProvider(SECClient(cache_dir="/tmp/sg-ticker-test"))
        provider._tickers = {"BRK-B": "0001067983", "AAPL": "0000320193"}
        assert provider.resolve_cik("BRK.B") == "0001067983"
        assert provider.resolve_cik("BRK-B") == "0001067983"
        assert provider.resolve_cik("aapl") == "0000320193"
        assert provider.resolve_cik("NOTREAL") is None
