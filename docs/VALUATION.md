# Scenario valuation

`stock_grader.valuation` provides a small, explicit DCF scenario engine. It is intentionally
separate from the historical grade: a factor score cannot become intrinsic value without
forward-looking assumptions.

The valuation primitives are Python APIs. The `research` command composes the default scenario
analysis into its dossier; there is no standalone `valuation` command.

```bash
stock-grader research AAPL \
  --universe peers.txt \
  --dcf-growth -0.02 0.05 0.12 \
  --discount-rate 0.10 \
  --terminal-growth 0.025 \
  --format md
```

The CLI exposes the three growth rates and the common discount/terminal rates. Use the Python API
for explicit-period length, annual dilution, direct scenario calls, or reverse-DCF bounds.

## Cash-flow definition

The available base is trailing:

```text
cash from operations - capital expenditure
```

Cash from operations is after interest, so this is treated as a levered, equity-oriented cash-flow
proxy and discounted at a required equity return. It is not canonical free cash flow to equity
(FCFE), because debt issued and principal repaid are not included.

The implementation deliberately does not:

- call the discount rate WACC;
- subtract net debt from the resulting present value;
- pretend this is enterprise-value FCFF;
- silently forecast working capital, margins, leverage, or reinvestment.

A full model should forecast either:

- cash flow to equity, paired with cost of equity; or
- cash flow to the firm, paired with cost of capital, followed by a complete bridge from enterprise
  value to equity.

Mixing an after-debt cash flow with WACC or a pre-debt cash flow with cost of equity creates a
denominator/numerator mismatch.

## Scenario API

```python
from stock_grader.valuation import DCFScenario, equity_cash_flow_value

scenario = DCFScenario(
    name="base",
    growth_rate=0.05,
    discount_rate=0.10,
    terminal_growth_rate=0.025,
    years=5,
    annual_dilution_rate=0.01,
)
result = equity_cash_flow_value(
    base_fcf=12_500_000_000,
    shares_outstanding=1_850_000_000,
    scenario=scenario,
    current_price=100.0,
)
print(result.to_dict())
```

`DCFResult` reports:

```text
scenario
value_per_share
current_price
upside_downside
forecast_cash_flows
forecast_shares
present_value_explicit
present_value_terminal
```

`upside_downside` is a mechanical comparison with the supplied current price, not a recommendation.

## Snapshot analysis

```python
from stock_grader.valuation import build_valuation_analysis

analysis = build_valuation_analysis(
    snapshot,
    growth_rates=(-0.02, 0.05, 0.12),
    discount_rate=0.10,
    terminal_growth_rate=0.025,
    years=5,
    annual_dilution_rate=0.01,
)
```

The three growth rates map, in order, to `bear`, `base`, and `bull`; pass exactly three values.
Defaults are:

| Assumption | Default |
|---|---:|
| Bear annual cash-flow growth | -2.0% |
| Base annual cash-flow growth | 5.0% |
| Bull annual cash-flow growth | 12.0% |
| Required equity return | 10.0% |
| Terminal growth | 2.5% |
| Explicit period | 5 years |
| Annual share dilution | 0.0% |

These are illustrative software defaults, not analyst estimates, calibrated forecasts, or
recommendations. Replace them with a thesis and show sensitivity to each.

The analysis is unavailable when:

- the business model is bank, insurance, or REIT;
- fundamentals are absent;
- positive trailing FCF is unavailable or stale;
- a positive usable share count is unavailable.

Banks and insurers require balance-sheet, capital, and regulatory models; REITs generally require
AFFO/NAV or property-level approaches. A generic corporate cash-flow proxy is refused rather than
forced.

## Reverse DCF

```python
from stock_grader.valuation import implied_growth_rate

growth = implied_growth_rate(
    current_price=100.0,
    base_fcf=12_500_000_000,
    shares_outstanding=1_850_000_000,
    discount_rate=0.10,
    terminal_growth_rate=0.025,
    years=5,
    annual_dilution_rate=0.01,
    lower=-0.50,
    upper=1.00,
)
```

The solver finds the constant explicit-period growth rate that reconciles the model with the
current price. It returns `None` rather than clipping when the price is outside the value range
spanned by the search bounds.

Interpret it as “the growth assumption required by this simplified model,” not the market's
observable forecast. Many combinations of margin, reinvestment, leverage, dilution, risk, and
terminal assumptions can support the same price.

## Mathematical structure

For explicit year \(t\):

```text
FCF_t    = base_fcf * (1 + growth_rate)^t
shares_t = shares_0 * (1 + annual_dilution_rate)^t
PV_t     = (FCF_t / shares_t) / (1 + discount_rate)^t
```

At year \(N\):

```text
terminal_cash_flow = FCF_N * (1 + terminal_growth_rate)
terminal_value     = terminal_cash_flow / (discount_rate - terminal_growth_rate)
```

The terminal value is converted to per-share value using year-\(N\) shares and discounted to the
present. The implementation requires:

- positive base FCF and shares;
- finite inputs;
- one to fifty explicit years;
- all rates greater than -100%;
- `discount_rate > terminal_growth_rate`.

The terminal value can dominate the result. Always review `present_value_explicit` and
`present_value_terminal` separately.

## Analyst adjustments not modeled

Before using the output, consider:

- normalized versus peak/trough base cash flow;
- stock-based compensation and economic dilution;
- working-capital reversals;
- maintenance versus growth capital expenditure;
- acquisitions and disposals;
- leases, pensions, minority interests, and preferred claims;
- debt principal flows and target leverage;
- excess cash and non-operating assets;
- taxes, cyclicality, currency, and country risk;
- fading returns on capital and terminal reinvestment;
- scenario-specific rather than constant discount rates.

The module does not solve these. Its purpose is to make a small set of assumptions inspectable.

## Primary valuation reference

Aswath Damodaran's NYU Stern teaching packet,
[Basics of Discounted Cash Flow Valuation](https://pages.stern.nyu.edu/~adamodar/pdfiles/eqnotes/basics.pdf),
updated January 2025, states the discounting-consistency principle: after-debt equity cash flows
must be paired with cost of equity, while pre-debt firm cash flows must be paired with cost of
capital. See particularly slides/pages 10 and 14–17. Reference checked 2026-07-28.

The same distinction appears in Damodaran's primary teaching page,
[Discounted Cashflow Models: What They Are and How to Choose the Right One](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/lectures/basics.html),
accessed 2026-07-28.
