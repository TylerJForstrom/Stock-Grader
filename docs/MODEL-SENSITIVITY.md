# Model-sensitivity interpretation

The report's `ci` field is retained for compatibility, but its correct name is
`sensitivity_interval`. It describes how the reported grade scale moves under a narrow family of
model perturbations. It is not a statistical confidence interval.

## Current calculation

For each security, the production pipeline currently:

1. takes the computed **pillar scores** and applicable profile weights;
2. draws new weight vectors from a Dirichlet distribution centered on those weights;
3. randomly omits about 15% of the components in each draw, retaining at least two;
4. re-aggregates each draw using the configured pillar aggregator and its parameters;
5. widens dispersion as observed metric coverage falls;
6. for cross-sectional and hybrid curves, re-ranks each target draw against the other securities'
   fixed point composites;
7. takes the 5th and 95th percentiles of the resulting reported-scale scenarios;
8. expands the endpoints when needed so the baseline reported score is included.

The default `GradeConfig` uses 300 draws and a fixed seed. The generic helper calls its inputs
`metric_scores`, but the current grade pipeline passes pillar scores. Therefore the production
interval perturbs top-level component weights and inclusion; it does not independently resample
every underlying metric or raw filing fact.

The peer membership and each other peer's point composite remain fixed.

## What “90%” means

Before baseline expansion, the endpoints are the central 90% of these simulated model scenarios.
This is a descriptive quantile range conditional on:

- the loaded snapshots;
- the chosen universe;
- the profile and available pillars;
- the normalizer and aggregator;
- the perturbation distribution;
- the coverage penalty;
- the random seed and number of draws.

It does not mean there is a 90% probability that an unknown true grade lies inside. A grade is a
model output, not a sampled physical parameter with an identified data-generating process.

Because the baseline is forcibly included, the displayed endpoints can be wider than the literal
5th–95th scenario interval.

## Letter scenario frequencies

Each draw is mapped to a letter using the configured curve. The resulting
`letter_scenario_frequencies` are also exposed under the compatibility name
`letter_probabilities`.

An entry such as:

```json
{"B": 0.56, "B-": 0.31, "C+": 0.13}
```

means 56% of the specified perturbation draws produced `B`. It does not mean:

- a 56% chance that the company is objectively a B;
- a 56% chance of a positive return;
- a calibrated probability of solvency, fair value, or analyst success.

## Curve effects

### Cross-sectional

The default reported score is the target's tie-aware peer percentile. Each sensitivity draw
replaces the target point among the other fixed peer composites and is re-ranked. This lets a
small composite change produce a discrete rank change, especially in a small or tied universe.

### Hybrid

Hybrid blends:

- the standardized composite; and
- the percentile derived from that composite in the same run.

Both are universe-dependent. The composite is not an independently calibrated intrinsic or
absolute score merely because the code/configuration calls its blend weight `absolute_weight`.

### Absolute

The legacy absolute curve applies fixed letter cutoffs to the standardized composite. With the
default cross-sectional normalizer, the inputs to that composite still depend on the loaded
universe. Fixed final cutoffs do not remove that dependence.

## What the interval includes

It is useful for asking:

- Does the result depend heavily on the exact pillar weights?
- Does dropping a small fraction of live top-level components change the result?
- Does lower evidence coverage make the model appropriately less stable?
- Do plausible composite changes cross peer-rank or letter boundaries?

A narrow interval supports only a narrow statement: this score is stable under these particular
perturbations, conditional on the current inputs and peer set.

## What the interval excludes

It does not model:

- wrong, restated, stale, or fraudulent filings;
- XBRL tag-selection or currency errors;
- errors in prices, shares, corporate actions, or benchmark data;
- a different or historically biased peer universe;
- uncertainty in peer membership;
- uncertainty in the peers' own resampled composites;
- alternative metric definitions or normalizers;
- structural model misspecification;
- regime change or future business performance;
- valuation growth, dilution, discount-rate, or terminal assumptions;
- future returns, tail losses, or portfolio risk.

These require separate data-quality, peer-set, specification, valuation, and out-of-sample tests.

## Coverage penalty and grade gates

The sensitivity distribution is widened as metric coverage falls. Separately, reports can be
gated to `N/A` when:

- total metric coverage is below the minimum;
- too little nominal profile weight remains usable;
- a required/defining pillar did not compute;
- no finite composite exists;
- the peer-relative curve has too few gradeable securities.

An `N/A` report can still contain a diagnostic numeric score or interval. The gate is authoritative.

## Internal calibration script

`scripts/calibrate_intervals.py` masks fundamental columns in the synthetic test universe, reruns
the model, and checks whether degraded-run intervals contain the corresponding full-data model
score.

That is a useful regression and missing-data stress test. It does not:

- observe a true latent stock grade;
- use a representative market population;
- validate future-return coverage;
- test alternative peer universes or data vendors;
- convert the sensitivity band into a frequentist or Bayesian interval.

Accordingly, internal hit rates from that script must not be presented as empirical 90% predictive
coverage.

## Recommended use

When presenting a grade, report:

- point score and letter;
- interval endpoints and the words “model sensitivity”;
- major letter scenario frequencies;
- peer universe and size;
- coverage, gates, effective/lost weights;
- at least one alternative peer-set or model-specification result.

Avoid the abbreviations “CI,” “confidence,” and “probability” unless the surrounding text
explicitly states the limited perturbation-scenario meaning.
