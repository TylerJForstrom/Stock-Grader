# Market data, provenance, and licensing

Stock-Grader combines public filing data with optional market-data sources. The repository's
software license does not grant rights to any third-party data. Provider terms, exchange rights,
redistribution restrictions, attribution requirements, privacy rules, and commercial-use limits
remain the user's responsibility.

This document is operational guidance, not legal advice. Terms change; review the current source
agreement before each production use.

## Fundamentals: SEC EDGAR

`SECProvider` uses:

- the SEC ticker-to-CIK map;
- `data.sec.gov/submissions/CIK##########.json`;
- `data.sec.gov/api/xbrl/companyfacts/CIK##########.json`.

The SEC describes these as unauthenticated JSON APIs for submissions and extracted XBRL facts.
Company Facts aggregates entity-wide disclosures associated with standard taxonomies; issuer
extensions, filing context, fiscal calendars, restatements, and differing tag choices still
require careful normalization.

Set a descriptive contact:

```bash
export STOCK_GRADER_CONTACT='your-name@example.com'
```

`SECClient` defaults to eight requests per second and caches responses. The SEC's current published
guideline limits a user to no more than ten requests per second across machines and asks automated
clients to identify themselves, download only what they need, and use efficient access. Large
research jobs should use the SEC's bulk archives rather than repeatedly calling issuer endpoints.

Point-in-time mode filters facts by filing date, but a complete audit still needs accession-level
lineage and source payload retention. Current research reports record selected canonical tags and
summary filing dates, not every fact context and accession.

### SEC-derived scalar prices

The optional SEC price path derives a scalar from reported insider transactions, with a constrained
public-float fallback. It is:

- sparse and transaction-derived, not an official daily closing-price feed;
- potentially stale;
- unsuitable for volatility, beta, momentum, liquidity, or total-return calculations;
- intended only to enable limited point-in-time valuation evidence when no better price is
  available.

The public-float fallback can be a lower bound when affiliates own shares. Read report warnings and
`price_source`, `price_date`, and `price_age_days`.

## Dense price providers

| CLI value | Credential | Intended role | Material caveats |
|---|---|---|---|
| `csv` | none | Caller-controlled daily data | Rights and adjustment quality are entirely caller-owned |
| `tiingo` | `TIINGO_API_KEY` | Authenticated daily OHLCV | Plan limits, internal-use, attribution, and redistribution terms apply |
| `yahoo` | none | Best-effort fallback | Unauthenticated endpoint; availability, schema, and permitted use can change |
| `stockanalysis` | none | Explicit opt-in fallback | Undocumented commercial endpoint; no project license or service guarantee |
| `sec` | none | Sparse scalar only | Not daily market history |
| `none` | none | Disable dense providers | SEC scalar remains on unless `--no-sec-prices` is used |
| `auto` | varies | Ordered convenience chain | Less reproducible than pinning a provider |

Provider failures are designed to fail soft. Missing daily data removes or weakens relevant
pillars; it must not be interpreted as evidence that risk or momentum is neutral.

### Auto order

`--price-provider auto` currently tries:

1. local CSV, when `--price-dir` is supplied;
2. StockAnalysis, only when the legacy `--stockanalysis` opt-in is supplied;
3. Tiingo, which succeeds only when configured;
4. Yahoo.

SEC-derived scalar pricing is a separate fallback controlled by `--sec-prices`.

For reproducible work, pin one provider and retain the downloaded vintage. `auto` can choose a
different source after a credential, outage, rate limit, or cache change.

## Price semantics

Stock-Grader distinguishes:

- `close`: traded close, used for the snapshot price and market capitalization;
- `adj_close`: split-and-distribution-adjusted series, used for return statistics.

A close-only CSV is accepted for compatibility. The validator copies it to `adj_close` and marks
the result `derived_from_close`; returns then exclude dividends and other distributions. That is
not suitable for a total-return backtest.

The validator reports:

- adjusted-price status;
- invalid and duplicate dates;
- non-positive prices and negative volume;
- internally inconsistent OHLC values;
- missing adjusted values;
- first and last observations;
- business-day coverage, longest gap, age, and staleness.

Validation can catch structural defects. It cannot verify a vendor's corporate-action factors,
timestamp convention, survivorship handling, or license.

## Provider-specific terms

### Tiingo

At the time of this documentation, Tiingo's terms state that API data is for internal consumption,
organizations must use an appropriate commercial plan, and redistribution requires permission.
They also prescribe attribution when redistribution is authorized. Confirm the terms and your
subscription before caching, sharing, publishing, or using the data in a service.

### Yahoo

The Yahoo adapter calls a chart endpoint without a contractual project integration. HTTP 429,
schema changes, regional restrictions, and access-policy changes are expected operational risks.
Review Yahoo's current terms and obtain permission appropriate to the use case. Do not treat a
successful HTTP response as a data license.

### StockAnalysis

The adapter is opt-in because it uses an undocumented endpoint. StockAnalysis's published terms
say accuracy is not guaranteed and restrict full republication of its content. The project has no
vendor agreement or SLA. Production use should replace this path with a licensed feed or obtain
explicit permission.

### FRED

FRED inputs support risk-free and benchmark calculations. FRED's legal page notes that individual
series can carry third-party copyrights and that API access does not override the original data
owner's restrictions. Check the copyright label and citation for each series.

The included benchmark series are price indexes, not total-return indexes. An alpha calculated
against them omits benchmark distributions and can be biased upward. They are not sufficient for
the `forward_return` field required by the backtest API.

## Manual and synthetic inputs

`--price TICKER=VALUE` is recorded as a manual scalar. Preserve the source, timestamp, currency,
and whether the quote is adjusted outside the current report; the CLI cannot infer them.

`--synthetic-prices` is for tests and demonstrations. The report labels synthetic history, but
that does not make any resulting metric usable for investment analysis or validation.

## Historical and backtest data requirements

An analysis-grade return panel should additionally provide:

- point-in-time security master and universe membership;
- permanent identifiers and complete symbol history;
- split, cash distribution, spin-off, and merger adjustments;
- delisting return or recovery proceeds;
- trading calendar and timestamp convention;
- bid/ask, borrow, liquidity, and implementable execution assumptions where relevant;
- vendor vintage and correction policy.

The included providers do not collectively satisfy that institutional data contract.

## Primary and provider references

References were checked on 2026-07-28:

- U.S. SEC, [EDGAR Application Programming Interfaces (APIs)](https://www.sec.gov/search-filings/edgar-application-programming-interfaces),
  published 2024-06-06 and last reviewed 2025-04-08.
- U.S. SEC, [Developer Resources](https://www.sec.gov/about/developer-resources), published
  2024-06-25 and last reviewed 2025-03-10. This is the source for the current ten-request-per-second
  fair-access guideline.
- U.S. SEC, [Inline XBRL](https://www.sec.gov/data-research/structured-data/inline-xbrl),
  originally published 2016-06-14 and last updated 2025-01-17.
- Tiingo, [Terms of Use](https://api.tiingo.com/tos/), last updated 2026-07-26.
- StockAnalysis, [Terms of Use](https://stockanalysis.com/terms-of-use/), last updated 2025-06-12.
- Yahoo, [Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html), accessed
  2026-07-28.
- Federal Reserve Bank of St. Louis,
  [FRED legal notices and API terms](https://fred.stlouisfed.org/legal/), accessed 2026-07-28.
