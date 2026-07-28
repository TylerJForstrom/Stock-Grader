# Quick start and CLI

This guide covers the interfaces that exist today. Research and backtesting have CLI commands and
Python APIs. Peer-selection and valuation primitives are also exposed through Python and are
composed by the `research` command.

## Install

Requirements:

- Python 3.11 or newer
- network access for uncached SEC filings
- a descriptive contact address for the SEC User-Agent
- optional, appropriately licensed market data for price-derived metrics

```bash
git clone git@github.com:TylerJForstrom/Stock-Grader.git
cd Stock-Grader
python -m venv .venv
```

PowerShell:

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
$env:STOCK_GRADER_CONTACT = "your-name@example.com"
```

macOS/Linux:

```bash
.venv/bin/python -m pip install -e '.[dev]'
export STOCK_GRADER_CONTACT='your-name@example.com'
```

The package entry point is `stock-grader`. Run `stock-grader --help` and
`stock-grader <command> --help` for the installed version's complete options.

Multiline command examples below use the POSIX `\` continuation. In PowerShell, place the command
on one line or replace each continuation with a backtick.

## Use an explicit universe

Create a UTF-8 text file with one ticker per line. Commas and whitespace are also accepted, and
text after `#` is ignored.

```text
# software-comp-candidates.txt
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

Then grade, rank, or compare profiles:

```bash
stock-grader grade AAPL --universe software-comp-candidates.txt --explain
stock-grader rank --universe software-comp-candidates.txt --profile quality --top 10
stock-grader consensus AAPL --universe software-comp-candidates.txt
stock-grader research AAPL --universe software-comp-candidates.txt --peer-mode auto
```

For `grade`, `rank`, and `consensus`, `--universe` is the actual comparison set. For `research`,
the default `--peer-mode auto` treats it as a candidate pool and invokes `select_peers`;
`--peer-mode explicit` retains its loaded members directly, aside from target/duplicate removal.

If `--universe` is omitted, `grade` and `consensus` load the bundled present-day convenience
universe. That is suitable for a smoke test, not a defensible comparable-company analysis. It is
blocked for sufficiently old `--asof` dates because using today's survivors in the past would
create survivorship bias.

`--no-peers` explicitly removes the comparison set. Under the default cross-sectional curve,
fewer than two gradeable securities results in `N/A`.

## Choose the model

Profiles are named presets:

```bash
stock-grader methods
stock-grader metrics
stock-grader metrics --pillar valuation
stock-grader grade AAPL --universe peers.txt --profile deep_value
```

The following overrides are available on `grade`, `rank`, and `consensus`:

```text
--weighting METHOD
--normalizer METHOD
--aggregator METHOD
--rho NUMBER
--curve absolute|cross_sectional|hybrid
--sector-neutral
```

The CLI exposes operational unsupervised weighting choices. Return-trained weighting methods must
be fitted on earlier periods and frozen outside this command path; they are not accepted as a
same-cross-section CLI shortcut.

The default profile curve is `cross_sectional`. See
[Model sensitivity](MODEL-SENSITIVITY.md) before interpreting `absolute` or `hybrid`; those names
do not make the underlying normalized composite intrinsic.

## Build a research dossier

```bash
stock-grader research AAPL \
  --universe software-comp-candidates.txt \
  --peer-mode auto \
  --peer-min 8 \
  --peer-max 30 \
  --size-band 5 \
  --profile quality \
  --dcf-growth -0.02 0.05 0.12 \
  --discount-rate 0.10 \
  --terminal-growth 0.025 \
  --format md
```

The three DCF growth values are bear, base, and bull decimal assumptions. The output includes the
peer manifest, grade, raw and normalized evidence, reported trends, data provenance, and scenario
valuation where inputs support it.

Use `--peer-mode explicit --universe FILE` when the file is already the reviewed final comp set.
Explicit mode requires `--universe`. Auto mode never expands beyond the candidate snapshots it
receives and therefore cannot repair a survivor-biased list.

## Point-in-time analysis

For a historical snapshot, provide both the date and `--pit`:

```bash
stock-grader grade AAPL \
  --asof 2025-03-31 \
  --pit \
  --universe universe-asof-2025-03-31.txt \
  --format json
```

Point-in-time mode keeps facts whose filing date was on or before the requested date. The caller
must also provide:

- universe membership that was knowable on that date;
- identifiers that account for delistings, acquisitions, and ticker reuse;
- prices and benchmark inputs aligned to that date.

For historical datasets, CIK is safer than ticker. The current CLI accepts ticker files; use
`SECProvider.fetch_by_cik` in a Python data-building workflow when delisted entities matter.

## Price inputs

### Local CSV

```bash
stock-grader grade AAPL \
  --universe peers.txt \
  --price-provider csv \
  --price-dir ./prices
```

The directory is searched for `TICKER.csv`. A `date,close` file is accepted, but its calculated
returns exclude dividends. For return, risk, and momentum work, provide a trustworthy
split-and-distribution-adjusted `adj_close` as well as the traded `close`.

Supported canonical fields are:

```text
date,open,high,low,close,adj_close,volume
```

Invalid dates, duplicates, non-positive prices, negative volume, inconsistent OHLC rows, sparse
coverage, and staleness are diagnosed by the price layer.

### Tiingo

```bash
export TIINGO_API_KEY='...'
stock-grader grade AAPL --universe peers.txt --price-provider tiingo
```

PowerShell uses `$env:TIINGO_API_KEY = "..."`.

Use only under a Tiingo plan that permits your intended use. Do not publish or redistribute
provider data merely because the library can download it.

### Other providers

```bash
stock-grader grade AAPL --universe peers.txt --price-provider yahoo
stock-grader grade AAPL --universe peers.txt --price-provider stockanalysis
stock-grader grade AAPL --universe peers.txt --price-provider sec
stock-grader grade AAPL --universe peers.txt --price-provider none --no-sec-prices
```

- Yahoo uses an unauthenticated chart endpoint and can rate-limit or change.
- StockAnalysis is an opt-in, undocumented commercial endpoint. `--stockanalysis` is retained as
  a compatibility shortcut.
- `sec` disables dense daily providers and permits only the sparse SEC-derived scalar-price path.
- `none` disables the dense chain, but SEC scalar prices remain enabled unless
  `--no-sec-prices` is also passed.

`auto` tries local CSV when `--price-dir` is supplied, opt-in StockAnalysis when
`--stockanalysis` is supplied, configured Tiingo, and then Yahoo. Provider failure degrades the
available metrics rather than aborting the grade.

See [Market data](MARKET-DATA.md) for source and licensing caveats.

### Manual scalar price

```bash
stock-grader grade AAPL --universe peers.txt --price AAPL=215.00
```

Repeat `--price TICKER=VALUE` for multiple securities. A scalar can unlock some valuation ratios,
but not statistics that require a daily series.

`--synthetic-prices` fabricates a labelled test series. Never use it for actual stock analysis.

## Caching and offline work

```bash
stock-grader grade AAPL --universe peers.txt --cache-dir ./cache
stock-grader grade AAPL --universe peers.txt --cache-dir ./cache --no-network
stock-grader grade AAPL --universe peers.txt --refresh
```

`--no-network` puts the SEC client in cache-only mode and disables network market-data inputs.
Missing cache entries lead to missing evidence; they are not silently replaced with fabricated
fundamentals.

## Evaluate a historical score panel

```bash
stock-grader backtest frozen-score-panel.parquet \
  --quantiles 5 \
  --min-cross-section 50 \
  --periods-per-year 12 \
  --transaction-cost-bps 20 \
  --bootstrap-samples 2000 \
  --bootstrap-block-periods 6 \
  --seed 7 \
  --format md
```

CSV and Parquet are supported. Every row needs the analytical columns:

```text
signal_date,return_start,return_end,ticker,score,forward_return
```

A strict CLI run also requires evidence columns:

```text
filed_through
universe_is_pit
return_is_total
delisting_return_included
one of: cik, security_id, permanent_id
```

Every attestation value must be true (`true`, `1`, `yes`, or `y`), the permanent identifier must
be populated, and `filed_through` must be populated and no later than `signal_date`.

`--allow-unverified-panel` permits an exploratory run when one or more evidence columns are
missing or false. The failed contract checks remain visible in the report. It does not bypass
invalid dates, filing dates after the signal, duplicate signal/ticker rows, impossible returns,
mixed outcome windows, or insufficient cross-sections.

See [Validation](VALIDATION.md) before interpreting the historical diagnostics.

## Output formats

```bash
stock-grader grade AAPL --universe peers.txt --format text
stock-grader grade AAPL --universe peers.txt --format md
stock-grader grade AAPL --universe peers.txt --format json
stock-grader rank --universe peers.txt --top 5 --format json
stock-grader consensus AAPL --universe peers.txt --format md
stock-grader research AAPL --universe peers.txt --format json
stock-grader backtest frozen-score-panel.csv --format json
```

JSON retains the legacy `ci` field and also emits the clearer `sensitivity_interval` name.
`letter_probabilities` are scenario frequencies under model perturbations, not probabilities of
future outcomes. See [Report schema](REPORT-SCHEMA.md).

## Before relying on an output

Check all of the following:

- Is the letter `N/A`?
- Do `gates` show insufficient metric or profile coverage?
- Is the universe economically comparable and recorded as-of the signal date?
- Are effective weights materially different from nominal weights?
- Are prices adjusted appropriately and current enough?
- Are warnings reporting missing pillars, stale facts, synthetic data, or a price-only benchmark?
- Does the sensitivity range cross several letter bands?

Grade, rank, consensus, and research return status 3 when no requested result is gradeable.
Treat the report fields as the explanation for that status.
