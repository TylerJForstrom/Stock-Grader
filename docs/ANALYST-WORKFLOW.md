# Analyst workflow

Stock-Grader is most useful as a structured first pass: it makes the comparison set, missing data,
model choices, and quantitative evidence visible before an analyst forms a conclusion.

## 1. Frame the question

Write down:

- the security and permanent issuer identifier;
- the investment style or profile being tested;
- the signal/as-of date;
- the intended holding horizon;
- the eligible market and business-model universe;
- whether the task is screening, valuation, monitoring, or historical validation.

A quality screen, a deep-value screen, and a distress screen are different questions. Profile
consensus is a view of that model disagreement, not permission to average away the thesis.

## 2. Freeze the information set

For current research, record the run date and source dates. For historical research:

- use `PitMode.PIT`;
- require every selected filing to have `filed <= signal_date`;
- use a universe constructed as of that date;
- key discontinued issuers by CIK;
- align prices, shares, and benchmark data to the same cutoff;
- retain the source files or immutable identifiers needed to reproduce the run.

The SEC's current ticker map omits many delisted issuers and tickers can be reused. A ticker-only
historical universe is therefore not a safe entity master.

## 3. Build an eligible candidate universe

Start from an investable set, not from companies that happen to survive today. Eligibility might
include exchange, country, security type, liquidity, reporting currency, and minimum filing
history. Record the rule and membership before looking at forward outcomes.

`select_peers` narrows an already loaded list using business model, SIC, currency, and size. It
does not discover an as-of universe or prove that the candidate list is free of selection bias.
See [Peer selection](PEER-SELECTION.md).

## 4. Attach market data deliberately

Fundamental ratios can run from EDGAR alone. Valuation, momentum, beta, liquidity, volatility, and
historical return validation require additional observations.

Prefer a source for which you have:

- contractual permission for the intended use and redistribution;
- raw traded closes for market capitalization;
- split-and-distribution-adjusted closes for total-return statistics;
- delisting returns or proceeds for backtests;
- an immutable snapshot or vendor vintage;
- corporate-action and symbol-history handling.

Close-only data is not a total-return series. A broad price index is not a total-return benchmark.
See [Market data](MARKET-DATA.md).

## 5. Grade and inspect the evidence

Use the CLI for a first screen:

```bash
stock-grader grade AAPL \
  --universe software-candidates.txt \
  --profile quality \
  --price-provider csv \
  --price-dir ./prices \
  --explain
```

Do not begin with the letter. Review, in order:

1. gates and `N/A` status;
2. peer/universe definition and as-of date;
3. filing, price, shares, and benchmark provenance;
4. coverage and missing/not-applicable distinctions;
5. nominal versus effective pillar weights and lost weight;
6. raw values and peer distributions;
7. strongest and weakest contribution terms;
8. model-sensitivity width and letter scenario frequencies;
9. sector-specific proxy warnings.

An effective weight is what actually affected this grade after unavailable pillars were removed
and remaining weights were renormalized. It can differ materially from the profile's nominal
thesis.

## 6. Build a research dossier

The CLI composes peer selection, evidence, trends, and scenario valuation:

```bash
stock-grader research AAPL \
  --universe software-candidates.txt \
  --peer-mode auto \
  --peer-min 8 \
  --peer-max 30 \
  --size-band 5 \
  --profile quality \
  --dcf-growth -0.02 0.05 0.12 \
  --discount-rate 0.10 \
  --terminal-growth 0.025 \
  --format md > AAPL-research.md
```

Use `--peer-mode explicit` when `--universe` already contains the reviewed final comps. Use the
Python API when snapshots come from a larger controlled data pipeline:

```python
from datetime import date
from pathlib import Path

from stock_grader.data.sec import SECClient, SECProvider
from stock_grader.peers import select_peers
from stock_grader.profiles import get_profile
from stock_grader.research import (
    build_research_report,
    research_to_json,
    research_to_markdown,
)
from stock_grader.types import PitMode

asof = date(2025, 12, 31)
provider = SECProvider(SECClient(contact="your-name@example.com"))
target = provider.fetch("AAPL", asof=asof, pit_mode=PitMode.PIT)

candidate_symbols = ["MSFT", "GOOG", "META", "ORCL", "ADBE", "CRM", "INTU", "IBM"]
candidates = [
    provider.fetch(symbol, asof=asof, pit_mode=PitMode.PIT)
    for symbol in candidate_symbols
]
peers, peer_manifest = select_peers(
    target,
    candidates,
    minimum=8,
    maximum=30,
    candidate_universe="software_candidates_2025-12-31",
)
report = build_research_report(
    target,
    peers,
    peer_manifest,
    get_profile("quality"),
    valuation_growth_rates=(-0.02, 0.05, 0.12),
    valuation_discount_rate=0.10,
    valuation_terminal_growth=0.025,
)

Path("AAPL-research.md").write_text(
    research_to_markdown(report),
    encoding="utf-8",
)
Path("AAPL-research.json").write_text(
    research_to_json(report),
    encoding="utf-8",
)
```

This example loads EDGAR fundamentals only. If the caller does not attach `price` and daily
`prices` to the snapshots, price-dependent metrics, return statistics, and reverse DCF will be
missing. The dossier records that absence; it does not fill it with a forecast.

## 7. Challenge valuation assumptions

The included DCF is deliberately separated from the grade. Treat each result as:

> If this after-interest cash-flow proxy grows at *g*, shares change at *d*, the required equity
> return is *r*, and terminal growth is *t*, then the mechanical present value is *x*.

It is not an analyst forecast. Normalize cyclicality, stock-based compensation, working capital,
capital intensity, acquisitions, and financing separately before relying on a value. Banks,
insurers, and REITs need different models and are refused by the API. See
[Scenario valuation](VALUATION.md).

## 8. Complete qualitative diligence

The system does not read the business like an analyst. At minimum, investigate:

- revenue drivers, unit economics, customer and supplier concentration;
- competitive advantage and likely erosion;
- management incentives, capital allocation, and related-party issues;
- segment economics hidden by consolidated XBRL;
- off-balance-sheet obligations, covenants, pensions, leases, and contingencies;
- accounting-policy changes and non-GAAP reconciliations;
- dilution, convertibles, options, and acquisition consideration;
- regulation, litigation, geopolitical exposure, and cyclicality;
- current guidance, consensus expectations, and identifiable catalysts.

The quantitative dossier should make these questions sharper, not replace them.

## 9. Validate without leakage

Freeze scores first and join them only to later outcomes. Use a survivorship-free universe,
corporate-action-adjusted total returns, delisting proceeds, costs, and chronological train/test
splits with an adequate embargo.

The `stock-grader backtest PANEL` command and its Python API evaluate a prepared panel. The strict
CLI contract requires `filed_through`, a populated permanent identifier, and all-true attestations
for PIT universe membership, total returns, and delisting inclusion. Neither interface sources the
panel, trains a model, or proves predictive accuracy. See [Validation](VALIDATION.md).

## 10. Record the decision

Preserve:

- configuration and profile;
- code version or commit;
- target and peer manifests;
- source/vintage information;
- raw research JSON;
- qualitative thesis and disconfirming evidence;
- valuation assumptions;
- expected horizon, risks, and monitoring triggers.

Re-running a model without preserving these items is a new analysis, not an audit of the old one.
