# Stock-Grader

Stock-Grader is an open-source, auditable stock-screening engine built around SEC EDGAR
fundamentals, optional market data, explicit peer universes, and configurable investment
profiles. It produces a peer-relative grade, the evidence behind it, and a model-sensitivity
range.

It is a research aid—not a return forecast, intrinsic-value oracle, portfolio optimizer, or
substitute for reading the filings.

## What is available

| Capability | Interface | Status |
|---|---|---|
| Grade one or more securities | `stock-grader grade` | CLI |
| Rank an explicit universe | `stock-grader rank` | CLI |
| Compare all investment profiles | `stock-grader consensus` | CLI |
| Build a peer/evidence/valuation dossier | `stock-grader research` | CLI + Python API |
| Evaluate a frozen historical score panel | `stock-grader backtest` | CLI + Python API |
| Join frozen panels to realized returns | `stock-grader build-panel` | CLI |
| Retract ledger records from trial accounting | `stock-grader ledger-retract` | CLI |
| Inspect registered methods and metrics | `stock-grader methods`, `metrics` | CLI |
| Deterministic comparable-company selection | `stock_grader.peers` | Python API + `research --peer-mode auto` |
| Scenario and reverse DCF primitives | `stock_grader.valuation` | Python API + research dossier |

There are no standalone `peers` or `valuation` commands; those capabilities are composed by
`research`.

## Quick start

Stock-Grader requires Python 3.11 or newer.

```bash
git clone git@github.com:TylerJForstrom/Stock-Grader.git
cd Stock-Grader
python -m venv .venv
```

PowerShell:

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
$env:STOCK_GRADER_CONTACT = "your-name@example.com"
.\.venv\Scripts\stock-grader --help
```

macOS/Linux:

```bash
.venv/bin/python -m pip install -e '.[dev]'
export STOCK_GRADER_CONTACT='your-name@example.com'
.venv/bin/stock-grader --help
```

The contact is sent in the EDGAR User-Agent. Keep it descriptive and respect the SEC's fair-access
guidance.

Start with an explicit universe:

```text
# peers.txt
AAPL
MSFT
GOOG
META
ORCL
ADBE
CRM
INTU
IBM
```

```bash
stock-grader grade AAPL --universe peers.txt --profile quality --explain
stock-grader rank --universe peers.txt --profile quality --top 10 --format md
stock-grader consensus AAPL --universe peers.txt --format json
stock-grader research AAPL --universe peers.txt --peer-mode auto --format md
stock-grader backtest frozen-score-panel.parquet --format md
```

Without `--universe`, `grade` and `consensus` use a bundled present-day convenience list. That
list is useful for trying the program, but it is not an automatically selected industry comp set
and must not be used for historical research. `--no-peers` normally produces `N/A` under the
default peer-relative curve.

For market data, choose a provider explicitly when reproducibility matters:

```bash
stock-grader grade AAPL --universe peers.txt --price-provider csv --price-dir ./prices
stock-grader grade AAPL --universe peers.txt --price-provider tiingo
stock-grader grade AAPL --universe peers.txt --price AAPL=215.00
```

`--price` supplies only a scalar valuation price. It does not create the daily history needed for
momentum, volatility, beta, or liquidity metrics. Tiingo requires `TIINGO_API_KEY`.

See [Quick start](docs/QUICKSTART.md) for point-in-time runs, price-file requirements, caching,
output formats, and common failure modes.

## What a grade means

The production default is cross-sectional:

1. metrics are computed from the snapshot available to the run;
2. usable metrics are normalized against the loaded universe;
3. metric scores roll up into pillars;
4. profile weights and an aggregator roll pillars into a composite;
5. the reported score and letter come from the security's percentile in that universe.

Consequently, a grade means “how this security screens under this profile, against this supplied
universe, using this data snapshot.” Change the universe, as-of date, data coverage, profile, or
provider and the grade may change.

`--curve hybrid` blends the standardized composite with its peer percentile. Both components are
derived from the run's universe; the blend is not half intrinsic or independently calibrated.
The legacy `--curve absolute` name means fixed letter cutoffs on the composite, but the composite
can still be peer-dependent because its normalizer is cross-sectional.

A report can return `N/A` when data or profile coverage is insufficient. Grade, rank, consensus,
and research return exit status 3 when none of the requested outputs is gradeable. Machine
consumers should still inspect `letter`, `gates`, `warnings`, coverage, peer provenance, and the
configured model.

The displayed 90% model-sensitivity range is the central range of explicit model perturbations,
expanded to include the baseline result. It is not a frequentist confidence interval, a price
range, or a probability statement about future returns. Letter frequencies have the same limited
interpretation. See [Model sensitivity](docs/MODEL-SENSITIVITY.md).

## Analyst workflow

A defensible workflow is:

1. state the research question, profile, as-of date, and eligible universe;
2. build that universe as it existed on the signal date, preferably keyed by CIK;
3. load filings in point-in-time mode and attach appropriately licensed price/return data;
4. select and record comparable companies;
5. inspect raw values, coverage states, effective weights, contributors, and provenance;
6. challenge valuation assumptions instead of treating scenarios as forecasts;
7. validate frozen historical scores only against later total returns with costs and delistings;
8. add qualitative work from the filing, industry structure, management, and known catalysts.

The `research` command assembles this path for CLI users:

```bash
stock-grader research AAPL \
  --universe peers.txt \
  --peer-mode auto \
  --peer-min 8 \
  --peer-max 30 \
  --dcf-growth -0.02 0.05 0.12 \
  --discount-rate 0.10 \
  --terminal-growth 0.025 \
  --format md
```

`--peer-mode explicit` retains the supplied universe members; `auto` applies the SIC,
business-model, currency, and size rules. The Python API exposes the same building blocks when the
caller already has snapshots:

```python
from datetime import date

from stock_grader.data.sec import SECClient, SECProvider
from stock_grader.peers import select_peers
from stock_grader.profiles import get_profile
from stock_grader.research import build_research_report, research_to_markdown
from stock_grader.types import PitMode

asof = date(2025, 12, 31)
provider = SECProvider(SECClient(contact="your-name@example.com"))

target = provider.fetch("AAPL", asof=asof, pit_mode=PitMode.PIT)
candidates = [
    provider.fetch(ticker, asof=asof, pit_mode=PitMode.PIT)
    for ticker in ["MSFT", "GOOG", "META", "ORCL", "ADBE", "CRM", "INTU", "IBM"]
]
peers, selection = select_peers(
    target,
    candidates,
    candidate_universe="research_universe_2025-12-31",
)
report = build_research_report(
    target,
    peers,
    selection,
    get_profile("quality"),
)
print(research_to_markdown(report))
```

This example does not attach daily prices, so price-dependent evidence and reverse valuation may
be unavailable. The candidate list is deliberately caller-owned: peer selection cannot repair
survivorship bias in the list it receives.

The backtest CLI requires a self-describing panel. In addition to signal/return dates, ticker,
score, and forward return, strict runs require `filed_through`, one of
`cik|security_id|permanent_id`, and all-true `universe_is_pit`, `return_is_total`, and
`delisting_return_included` columns. See [Validation](docs/VALIDATION.md) before using
`--allow-unverified-panel`.

Panel reads are manifest-verified. `freeze` writes a `manifest.json` catalog beside each
`frozen_scores/<profile>/` directory (per-part sha256, rows, columns, and a dataset-content
version distinct from the manifest-format version), and `build-panel` does the same for its
output directory. Every panel-consuming path — `backtest`, `ledger-declare`, `build-panel`,
`decay` — checks a panel's sha256 against the sibling manifest when one exists and refuses on
mismatch; a directory with no manifest predates the convention and loads with a warning (old
panels are immutable and are never rewritten to add one — the next freeze catalogs them
additively, marked `backfill`).

## Documentation

- [Quick start and CLI](docs/QUICKSTART.md)
- [Analyst workflow](docs/ANALYST-WORKFLOW.md)
- [Peer selection](docs/PEER-SELECTION.md)
- [Grade and research-report schemas](docs/REPORT-SCHEMA.md)
- [Market-data providers and licensing](docs/MARKET-DATA.md)
- [Model-sensitivity interpretation](docs/MODEL-SENSITIVITY.md)
- [Leakage-aware validation and backtesting](docs/VALIDATION.md)
- [Scenario valuation](docs/VALUATION.md)
- [Current limitations](docs/LIMITATIONS.md)

The files under `docs/design/` are design notes and historical investigation. They are not a list
of guaranteed shipped interfaces.

## Development

```bash
python -m pytest
python -m ruff check src tests scripts
python -m mypy src
```

The package is beta software. Review the methodology, data rights, and generated evidence before
using it in any decision process. Nothing in this repository is investment, legal, tax, or
accounting advice.
