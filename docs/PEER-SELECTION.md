# Peer selection

The peer set is part of the model. Stock-Grader's default normalization and grade curve compare a
security with other securities loaded in the same run, so peer membership can change raw metric
scores, pillar scores, rank, and letter.

## Interfaces and modes

The project currently has:

- `grade`, `rank`, and `consensus`, where `--universe` is used directly as the comparison set;
- `research --peer-mode auto`, where `--universe` is a candidate pool for `select_peers`;
- `research --peer-mode explicit`, which records the supplied members without automatic
  business-model, currency, fundamentals, SIC, or size exclusions;
- API functions `select_peers` and `explicit_peers`.

There is no standalone `peers` command. The bundled default universe remains a convenience list;
automatic selection can only narrow the candidates it receives.

```bash
stock-grader research AAPL \
  --universe candidate-universe.txt \
  --peer-mode auto \
  --peer-min 8 \
  --peer-max 30 \
  --size-band 5 \
  --format md

stock-grader research AAPL \
  --universe reviewed-final-comps.txt \
  --peer-mode explicit \
  --format json
```

## API

```python
from stock_grader.peers import PeerSelection, select_peers

peers, manifest = select_peers(
    target,
    candidates,
    minimum=8,
    maximum=30,
    size_band_multiple=5.0,
    candidate_universe="us_operating_companies_2025-12-31",
)
```

Signature:

```python
select_peers(
    target: SecuritySnapshot,
    candidates: list[SecuritySnapshot],
    *,
    minimum: int = 8,
    maximum: int = 30,
    size_band_multiple: float = 5.0,
    candidate_universe: str = "caller_supplied",
) -> tuple[list[SecuritySnapshot], PeerSelection]
```

Validation requires `minimum >= 2`, `maximum >= minimum`, and `size_band_multiple > 1`.

For a caller-authored final comp set:

```python
from stock_grader.peers import explicit_peers

peers, manifest = explicit_peers(
    target,
    reviewed_candidates,
    candidate_universe="reviewed_comps_2025-12-31",
)
```

Explicit mode removes only the target and duplicate tickers. It warns, but does not filter, when
membership crosses business-model classes or contains fewer than two peers.

## Automatic eligibility and ordering

A candidate is excluded when it:

- is the target security;
- duplicates an earlier ticker;
- has no usable `fundamentals` object;
- reports in a different currency;
- belongs to a different `SectorClass`.

`SectorClass` is the coarse business-model guard used by the metric applicability layer:
`bank`, `insurance`, `reit`, `holding`, `utility`, `energy`, or `general`. This prevents a bank
from being widened into a general industrial merely to hit a minimum count. It does not make every
remaining general-company comparison economically sensible.

Eligible candidates are sorted deterministically by:

1. absolute log market-cap distance from the target; then
2. uppercase ticker.

Unknown market capitalization sorts behind known size comparisons.

## Widening rule

Selection accumulates candidates through these passes:

1. same four-digit SIC and within the size band;
2. same three-digit SIC and within the size band;
3. same two-digit SIC and within the size band;
4. same business-model class and within the size band;
5. repeat SIC4, SIC3, SIC2, and business-model passes with the size requirement relaxed.

The routine stops after a pass reaches the requested minimum or the maximum is filled. It returns
fewer than `minimum` rather than crossing the business-model boundary. A warning records the
shortfall.

This is a deterministic rule, not an assertion that SIC is a perfect economic-industry ontology.
Revenue mix, geography, capital intensity, vertical integration, growth stage, and accounting
choices can still make a selected comp weak.

## Selection manifest

`PeerSelection.to_dict()` contains:

```text
target
asof
rule
candidate_universe
members
widening_steps
excluded
warnings
fingerprint
```

The SHA-256 fingerprint covers target, as-of date, rule, candidate-universe label, selected
members, and widening steps. It does not hash the full candidate snapshots, source payloads,
exclusion map, or code version. Preserve those separately for a complete audit.

Use a stable, meaningful `candidate_universe` identifier, such as a dated database snapshot or
manifest checksum—not a label like `final-list`.

## What the caller must do

`select_peers` cannot:

- discover securities that existed on a historical date;
- restore delisted or acquired issuers omitted upstream;
- recognize ticker reuse;
- convert currencies;
- infer segment revenue similarity;
- test whether market prices were actually investable;
- remove outcome-based or researcher selection bias.

For historical work, build the candidate universe as of the signal date and prefer permanent CIKs
upstream. Fetch every candidate using the same point-in-time cutoff.

## Minimum peer count

Eight is a pragmatic default, not a statistical guarantee. A small or tied universe produces
coarse percentile ranks, unstable quartiles, and large rank jumps under small changes. Conversely,
a very broad set can improve numerical smoothness while weakening economic comparability.

Record both:

- the eligible candidate universe; and
- the final peer set.

Sensitivity to plausible alternative peer definitions should be a separate robustness check. The
reported model-sensitivity interval holds peer membership fixed and does not measure this risk.
