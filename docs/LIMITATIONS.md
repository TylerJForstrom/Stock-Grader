# Current limitations

Stock-Grader is beta research software. The points below are boundaries of the current
implementation, not edge-case disclaimers.

## Interpretation

- A grade is a configurable peer-relative screen, not a forecast of return, fair value, distress,
  or business quality.
- The default score is a percentile in the supplied gradeable universe.
- The hybrid curve's composite and percentile are both universe-dependent.
- The legacy absolute curve uses fixed final cutoffs but can still use peer-normalized inputs.
- Profile consensus measures agreement among authored lenses. Its `clarity` is a heuristic, not a
  confidence score.

## Peer and universe risk

- Grade, rank, and consensus use the explicit universe directly; `research --peer-mode auto`
  performs automatic selection from its supplied candidate pool.
- The bundled universe is a present-day convenience list, not an industry comp set or historical
  membership database.
- The dated wide universe is a separate forward record. It must not be pooled with the bundled
  universe: the population, cross-sectional percentiles, and portfolio breadth differ.
- Fixed percentile letter cutoffs change their practical breadth with universe size. An A+ covers
  roughly two or three names at N=82 but about 30 at N=1,000; letters are not comparable across
  those universes.
- `select_peers` narrows caller-supplied snapshots and cannot fix survivorship or selection bias
  upstream.
- SIC and coarse business-model classes do not capture segment mix, geography, life-cycle stage,
  capital intensity, or accounting comparability.
- Business-model sector neutralization retains a large GENERAL catch-all. The additive `sic2` and
  `sic3` alternatives require a separately pre-registered trial; the production default remains
  unchanged. In the measured 1,000-name universe, GENERAL contains 703 names, while SIC2 and SIC3
  create 8 and 46 singleton groups and put 266 and 500 names in groups smaller than 15,
  respectively; finer labels reduce concentration but do not automatically improve statistical
  comparability.
- Coverage is universe-dependent because the denominator includes metrics computable by at least
  one member of the run. Adding mid-caps can make more metrics enter that denominator and can
  reduce otherwise identical companies' reported coverage.
- Peer membership is fixed inside the reported sensitivity simulation.

## Fundamental data

- SEC Company Facts is an aggregated XBRL source, not a fully normalized institutional fundamentals
  database.
- Issuer-specific extensions, inconsistent tags, context choices, fiscal calendars, amendments,
  and restatements can still produce gaps or errors.
- Foreign-currency facts are not converted; incompatible facts are omitted rather than silently
  treated as USD.
- Current provenance identifies canonical tag choices but not complete accession/context lineage
  for every metric input.
- XBRL does not capture every economically important disclosure in footnotes, exhibits, narrative,
  regulatory schedules, or segment tables.
- PIT filtering cannot repair a present-day survivor universe or vendor data that was revised
  without retained vintages.

## Market data

- No bundled provider constitutes a guaranteed, exchange-licensed institutional feed.
- Yahoo and StockAnalysis integrations are operationally and contractually fragile.
- Tiingo use is subject to the user's plan and current terms.
- SEC-derived prices are sparse transaction-based scalars, not daily closes.
- Close-only histories exclude distributions even when copied into an `adj_close` compatibility
  column.
- FRED benchmark series used here are price indexes, not total-return indexes.
- Manual scalar prices lack automatic source, currency, and timestamp verification.
- Synthetic prices are never evidence.

See [Market data](MARKET-DATA.md).

## Metric and model risk

- A large metric registry does not guarantee independent information; many ratios share the same
  accounting inputs.
- Normalization, winsorization, weighting, aggregation, profile definitions, and gates are model
  choices.
- Dynamic weighting can favor dispersion or covariance structure without establishing economic
  causality or out-of-sample value.
- Sector-specific metrics contain proxies. For example, regulatory bank capital and canonical NIM
  may not be available from standardized XBRL facts.
- Missing and not-applicable handling is explicit, but effective weights can cause a grade to rest
  on a substantially narrower thesis.
- Qualitative information, management incentives, competitive dynamics, catalysts, estimates, and
  market expectations are largely outside the model.

## Sensitivity interval

- The “90%” range is a 5th–95th model-scenario range expanded to include the baseline.
- The current production call perturbs top-level pillar weights and component inclusion, not each
  raw metric independently.
- It excludes peer-set uncertainty, source error, revisions, model misspecification, and future
  outcomes.
- Letter frequencies are not calibrated outcome probabilities.
- The internal masking script tests model self-consistency, not statistical or predictive
  coverage.

See [Model sensitivity](MODEL-SENSITIVITY.md).

## Valuation

- The DCF uses trailing CFO minus capex, an after-interest levered proxy that omits debt principal
  flows and is not canonical FCFE.
- Default growth, discount, terminal, and dilution rates are illustrations.
- Constant growth and constant discount-rate assumptions are intentionally simple.
- The model does not normalize cyclicality or fully model reinvestment, leverage, stock
  compensation, non-operating assets, or contingent claims.
- It refuses banks, insurers, and REITs but does not supply the specialized replacement models.
- Reverse DCF identifies one implied growth rate conditional on all other assumptions; it does not
  recover a unique market expectation.

See [Valuation](VALUATION.md).

## Validation and backtesting

- No bundled survivorship-free point-in-time panel or audited out-of-sample return result exists.
- Distress AUC scripts measure separation in a queried sample with hand-selected controls; they do
  not prove predictive accuracy.
- The backtest evaluator assumes the caller has already built correct scores and total returns.
- The strict CLI contract checks required columns and caller attestations; it does not independently
  verify their truth.
- The evaluator keys duplicate detection and turnover membership by a permanent-ID column
  (`cik`, `security_id`, or `permanent_id`) whenever the panel supplies one complete, falling
  back to `ticker` otherwise — so panels without a permanent identifier must still normalize
  historical symbol changes upstream.
- It does not automatically prevent outcome-window overlap across signal dates.
- Quantile portfolios are equal-weight diagnostics, and the fixed turnover charge omits market
  impact, borrow, capacity, taxes, and execution detail.
- Bootstrap intervals summarize the supplied historical periods; they do not describe future
  performance.
- Multiple-testing and model-selection bias remain the researcher's responsibility.

See [Validation](VALIDATION.md).

## Reporting and operations

- `ResearchReport` has a `1.0` schema version, but CLI grade/consensus JSON does not yet have a
  formal versioned JSON Schema.
- The compatibility field `ci` remains even though “confidence interval” is the wrong
  interpretation.
- Research and backtesting have CLI commands. Peer-selection and valuation primitives are exposed
  through Python and composed by `research`; there are no standalone `peers` or `valuation`
  commands.
- Caches are mutable local files rather than an immutable, content-addressed data lake.
- Outputs do not automatically record the code commit, dependency lock, source payload hashes, or
  data-license entitlement.
- Provider outages generally fail soft, which preserves a report but can materially change its
  evidence and effective weights.

## Not implemented as an investment platform

The project does not currently provide:

- brokerage integration or order execution — a deliberate stop, not a gap: OUT OF SCOPE by
  ecosystem decision (Stock-Data `ECOSYSTEM.md` decision log, 2026-08-04, and its rule 8 money
  boundary) until a gate declared under the versioned promotion policy (PROMOTION-POLICY v1,
  `docs/PROMOTION.md`, declared by sha256 in `research_ledger.jsonl`) passes; revisiting
  requires a new decision-log entry there;
- portfolio construction, optimization, or tax-lot accounting — same recorded decision;
- real-time quotes or alerts;
- estimate revisions, transcripts, news, or alternative data;
- complete security-master and corporate-action history;
- scenario-specific macro/industry forecasting;
- analyst approval controls, immutable audit logs, access control, or regulatory compliance
  workflows.

## Appropriate use today

Use Stock-Grader to:

- create a reproducible quantitative screen;
- identify missing evidence and model disagreement;
- compare raw metrics with a consciously chosen peer set;
- generate questions for filing and industry diligence;
- test a properly sourced historical panel through a transparent evaluation API.

Do not use a grade, sensitivity interval, scenario value, or diagnostic AUC alone to make or market
an investment decision.

## Forward panel construction

- `forward_return` includes per-ex-date cash dividends from the private
  vault's dividend archive when it is present and covers the window months;
  `return_is_total` attests True only when the measured row coverage is
  >= 99%. Without the archive the return stays price-only with the
  attestation honestly False (understating returns for high-yield names).
  Rows whose cash cannot sit on the entry share basis (a mid-window split
  with in-window ex-dates, non-USD cash) stay price-only and are counted
  uncovered per row (`dividend_covered`).
- Names that leave listed venues are held at the last listed close (the archive
  excludes OTC), which slightly overstates the return of names that continued
  falling off-exchange. The convention is disclosed per row via
  `terminal_price_used`.
- Splits without a foundry record are reconstructed from the price+volume
  signature; the correction is flagged per row (`split_source =
  "reconstructed"`) and an uncorroborated split-shaped move excludes the row
  rather than guessing.
