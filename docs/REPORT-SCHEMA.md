# Report and evidence schemas

Stock-Grader exposes Python dataclasses and JSON serializers. This is a documented application
schema, not a published JSON Schema standard, and the package is still beta. Persist the package
version or commit alongside every output.

## GradeReport

`grade_universe` returns `dict[str, GradeReport]`; `grade_one` returns one `GradeReport`.

| Field | Meaning |
|---|---|
| `ticker`, `asof`, `profile` | Security, information date, and named model preset |
| `score`, `letter` | Reported score and letter under the configured curve |
| `pillars` | Mapping of pillar name to `PillarScore` |
| `pillar_weights` | Profile's nominal weights |
| `effective_pillar_weights` | Weights after unavailable pillars are removed and live weights renormalized |
| `lost_weight` | Nominal profile weight that could not be applied |
| `percentile` | Tie-aware percentile in the gradeable run universe, when available |
| `ci` | Backward-compatible storage name for the model-sensitivity interval |
| `coverage` | Computed share of metrics that were knowable for this run |
| `weighting_method`, `normalizer`, `aggregator` | Model configuration labels |
| `gates` | Conditions that caused the report to be ungradeable |
| `explain` | Contribution, count, universe, and scenario details |
| `warnings` | Human-readable data and model caveats |
| `meta` | Sector, PIT mode, curve, pillar set, and interval metadata |

Properties:

- `graded` is false when `letter == "N/A"`.
- `sensitivity_interval` aliases `ci`.
- `letter_probabilities` reads the scenario frequencies stored in `explain`.
- `top_contributors()` returns signed metric contributions, using the exact decomposition when
  available and effective pillar weights as the fallback.

`stock_grader.report.to_json` adds both `sensitivity_interval` and `letter_probabilities` to the
serialized grade while retaining `ci` for compatibility.

### PillarScore

Each `pillars[name]` contains:

```text
pillar
score
weights
contributions
metric_scores
coverage
n_metrics
n_missing
n_not_applicable
weighting_method
aggregator
warnings
```

`weights` and `contributions` at this level describe metrics inside that pillar. The exact
top-level metric attribution is also emitted in `GradeReport.explain.metric_contributions`.

### Coverage states

Raw metric evaluation distinguishes:

- `ok`: computed and usable;
- `missing`: applicable but unavailable or invalid;
- `not_applicable`: undefined for that business model.

`not_applicable` is not treated as a download failure. For example, a current ratio is not a
meaningful missing field for a bank that does not publish a classified balance sheet.

### Explain object

Current grade reports include:

```text
pillar_contributions
metric_contributions
standardized_composite
peer_rank_curve_effect
attribution_residual
n_metrics_ok
n_metrics_missing
n_metrics_not_applicable
n_metrics_run_limited
n_metric_errors
metric_errors
metrics
universe_size
lost_weight
profile_weight_coverage
required_pillars
letter_scenario_frequencies
letter_probabilities
```

`letter_probabilities` is a compatibility name. These values are frequencies among explicit model
perturbations, not probabilities that a company is “really” a given grade or that an investment
will succeed.

`explain.metrics` maps every evaluated metric name to:

```text
pillar, raw_value, unit, coverage, note, raw_inputs
normalized_score
effective_metric_weight
effective_pillar_weight
contribution
```

This full evidence map is distinct from the shorter strongest/weakest presentation.

The contribution reconciliation is:

```text
reported score - 50
  ~= sum(metric contributions)
   + peer-rank curve effect
   + attribution residual
```

Nonlinear aggregators, missing components, and the final peer-rank transformation make the
separate curve effect and residual important.

### Meta object

Current metadata includes:

```text
schema_version, run_status
sector, industry, sic, cik, company_name, currency
pit_mode, curve, score_interpretation
pillar_set, coverage_penalty, profile_weight_coverage, interval_kind
config, config_fingerprint
universe_fingerprint, universe_members, effective_peer_count
price_source, price_date, price_age_days, price_is_adjusted
shares_source, shares_date
benchmark, benchmark_is_price_only
```

The configuration fingerprint hashes the serialized grading settings. The universe fingerprint
hashes ticker, CIK, as-of date, and business-model class for the loaded snapshots. Neither hashes
the source facts or price frames.

## JSON shapes by CLI command

- `grade` with one ticker emits one grade object.
- `grade` with multiple tickers emits a ticker-to-grade mapping.
- `rank` emits a ticker-to-grade mapping inserted in ranking order after `--top` is applied. JSON
  consumers should not treat object-member order as a formal rank field.
- `consensus` with one requested ticker emits one consensus object.
- `consensus` with multiple requested tickers emits a ticker-to-consensus mapping.
- `research` emits one `ResearchReport 1.0` object.
- `backtest` emits one `BacktestReport` object.

Do not infer gradeability from numeric `score` alone. Inspect `letter` and `gates`.

## ConsensusResult

The consensus object contains:

```text
ticker
score
letter
clarity
spread
best_profile
worst_profile
scores
per_profile
```

Only finite, graded profile reports enter `scores`, the arithmetic mean, spread, and clarity.
`N/A` profile reports remain in `per_profile` for audit but are excluded from aggregation.

`clarity = clip(100 - 2.5 * spread, 0, 100)`. It is a model-agreement heuristic, not a measured
confidence level. The consensus letter is taken from the included profile whose score is nearest
the median, preserving that report's configured grade curve. It may therefore not equal a fixed
cutoff applied to the arithmetic mean.

## ResearchReport 1.0

`build_research_report` returns:

| Field | Meaning |
|---|---|
| `schema_version` | Currently `"1.0"` |
| `company` | Ticker, name, CIK, SIC, industry, business model, currency, price, shares, market cap |
| `grade` | Full `GradeReport` |
| `peer_selection` | Full `PeerSelection` manifest |
| `provenance` | Snapshot, filing, price, shares, benchmark, concept-tag, and peer identifiers |
| `metrics` | One `MetricEvidence` record per evaluated target metric |
| `trends` | Up to five recent reported fiscal observations for selected concepts and ratios |
| `valuation` | `ValuationAnalysis` with scenarios, assumptions, and warnings |
| `interpretation` | Deliberately limited screen description |
| `warnings` | Deduplicated peer, grade, valuation, and research caveats |

Serialize with:

```python
from stock_grader.research import research_to_json, research_to_markdown

json_text = research_to_json(report, indent=2)
markdown_text = research_to_markdown(report)
```

The CLI serializer is:

```bash
stock-grader research AAPL --universe peers.txt --peer-mode auto --format json
```

### MetricEvidence

Each metric evidence record includes:

```text
name, pillar, description
raw_value, unit, coverage, missing_reason
normalized_score
metric_weight, effective_pillar_weight, contribution
peer_median, peer_q25, peer_q75, peer_percentile, usable_peers
direction, note, raw_inputs
```

Peer percentiles use a tie-aware midrank after inserting the target among usable peers. Peer
distribution fields are absent when no usable raw peer values exist.

### Provenance boundaries

The dossier records:

- as-of and PIT mode;
- latest eligible fiscal period and filing date;
- price source/date/age and adjusted flag;
- shares source/date;
- benchmark and whether it is price-only;
- canonical concept-to-XBRL-tag choices;
- peer-selection fingerprint.

This is useful but not complete data lineage:

- `canonical_concept_tags` names the chosen tag, not every source fact's accession number,
  context, unit, or amendment chain;
- `latest_filing_date` summarizes the normalized snapshot and is not a per-metric vintage;
- `raw_inputs` exists only where a metric supplies it and may contain derived rather than original
  facts;
- provider responses and license entitlements are not embedded;
- the code commit and environment are not automatically recorded.

For regulated or production use, preserve source payload hashes, accession-level fact lineage,
provider/vintage identifiers, configuration, dependency lockfile, and code commit externally.

## BacktestReport

`evaluate_walk_forward` and `stock-grader backtest` serialize:

```text
config
input_contract
periods
observations, rejected_periods
mean_rank_ic, rank_ic_information_ratio, rank_ic_positive_rate
mean_gross_spread, mean_net_spread, spread_positive_rate
annualized_net_spread, annualized_spread_sharpe
max_drawdown, mean_turnover, quantile_monotonicity
rank_ic_interval, net_spread_interval
limitations
adv_band
```

`adv_band` is `null` for every panel that is not one band of a pre-registered ADV partition —
frozen score panels, synthetic calibration grids, and any signal panel built from an unbanded
observation dataset. When it is present it carries the band's identity (`band_id` and the
dollar edges in `band`), the pre-registration it was cut under, the declared
`min_evaluable_periods` floor, this panel's `evaluable_periods`, and a `reportable` verdict with
`not_reportable_because` naming every leg that failed. `reportable: false` means the programme
licenses no band statistic from the panel: `stock-grader backtest` exits 2 rather than printing
one, and `--allow-unreportable-band` is required to get an explicitly caveated exploratory run
whose report leads with a `NOT REPORTABLE` limitation and whose ledger line records it.

`input_contract` has five booleans:

```text
filing_cutoff_provided
point_in_time_universe_attested
total_returns_attested
delistings_included_attested
permanent_identifier_present
```

The strict CLI rejects a report with any false contract value unless
`--allow-unverified-panel` is explicit. The Python evaluator always returns the contract and
limitations but does not enforce that CLI policy itself.

## Compatibility policy

`ResearchReport` carries top-level `schema_version = "1.0"` and grades carry
`meta.schema_version = "1.0"`. There is not yet a formal published JSON Schema or a migration
contract for every CLI wrapper object. Until those are added:

- tolerate additional fields;
- avoid positional assumptions;
- treat absent optional values as expected;
- retain `ci` support while preferring `sensitivity_interval`;
- pin a tested package commit for downstream automation.
