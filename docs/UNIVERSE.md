# Frozen-universe policy

Stock-Grader maintains two deliberately separate forward records:

- `frozen_scores/` keeps the original 82-name convenience universe running so
  its existing forward history remains comparable.
- `frozen_scores_wide/` uses an immutable, dated 1,000-name selection. A wide
  panel is comparable only with panels carrying the same `universe_id`,
  `universe_spec_sha256`, `universe_fingerprint`, and configuration
  fingerprint.

The wide-universe rule is pre-registered in
`config/universe_spec.json`. The dated membership file is committed rather
than regenerated during a freeze because the public GitHub runner has no
access to the private Vault and because recomputing membership would make an
old panel impossible to reproduce.

## Licensing decision and the two screen artifacts

M5 offered two publication choices. This branch uses the public-domain
fallback, option B. The implementation decision was made by the Codex M5 agent
on 2026-07-30 after checking Massive's then-current Market Data Terms; the
project owner had not supplied a separate redistribution licence or an
existing decision authorising option A. Section 5(c) expressly covers works
derived from the market data, so this public repository receives no
Massive-derived values, ranks, rank order, or membership.

The public membership therefore ranks SEC identifiers by
`dei:EntityPublicFloat`, a US-government public-domain fact:

1. Start from the manifest-verified Stock-Data symbol snapshot.
2. Keep the listed exchanges named in the spec, exclude ETFs and test issues,
   and require an SEC CIK.
3. Group eligible canonical SEC-dash tickers by CIK and retain the
   lexicographically smallest ticker, so one issuer occupies at most one slot.
4. For each CIK, use the latest positive finite `EntityPublicFloat`
   observation whose measurement end and SEC filed date are both on or before
   the selection date.
5. Rank descending by public float, break ties by canonical SEC-dash ticker
   ascending, keep 1,000, and sort the emitted membership alphabetically.

The membership header's `source_sha256` hashes canonical JSON containing the
SEC ZIP SHA-256 and the full verified Stock-Data current-symbol and event
manifests. It therefore identifies every input that can change membership, not
only the bulk archive.

The legacy `liq1000_v1` identifier is retained because it is the milestone's
pre-registered experiment name; it does not imply that vendor-derived
liquidity values are public.

Stock-Vault separately computes the original liquidity diagnostic for private
research. For an as-of date T it reads each of the last 63 archived trading-day
files strictly before T exactly once, computes each symbol's median
`close * volume`, requires at least 40 observations and a median close of at
least $5, excludes ETFs and test issues, requires a listed SEC CIK, ranks
descending with ticker-ascending ties, and keeps the configured target. Its
gzipped ranked rows and all numeric values remain in the private Vault. The
optional text emitter exists for reproducibility testing but is not the source
of the public repository's membership under option B.

## Point-in-time limits

A dated universe file may be used only on or after its header `asof`; it is
never overwritten. The loader refuses a future selection and refuses a
selection more than 365 days away from the requested signal date.

The private whole-market archive starts in 2024-07, so the liquidity diagnostic
cannot honestly reconstruct a universe before that month. The public SEC-float
rule is forward-only from the first archived symbol snapshot and SEC bulk
generation used to create it. Current companyfacts contains filing history,
but a current bulk download is not itself a vintage archive of bytes that
existed years ago. Neither rule is described as a historical 2015 universe.

## What changes at 1,000 names

A letter remains a cross-sectional rank, not an intrinsic statement about a
company. Under the fixed percentile cutoffs, A+ means at or above the 97th
percentile: roughly two or three names in an 82-name panel but about 30 names
in a 1,000-name panel. Wide and narrow letters therefore do not mean the same
thing and must not be pooled.

The 15-name letter floor remains unchanged. It is still an outage interlock,
but it almost never binds once a healthy 1,000-name cross-section exists.
Percentile cutoffs are intentionally unchanged so the universe expansion is
not confounded with a second scoring-method change.

`sector_neutral_key` is additive and defaults to `business_model`, the prior
seven-bucket behaviour. `sic2` and `sic3` are available for separately
pre-registered trials; this milestone does not silently change the production
default.

The public monthly runner uses SEC-only prices. SEC provides useful sparse
price evidence but not a dense daily series, so the momentum and
low-volatility profiles correctly refuse when their defining price pillars are
absent. The safety gates are not weakened to manufacture two panels. A local
command can add `--vault C:/Users/tforstrom/Desktop/Stock-Vault` after the
`freeze` subcommand to obtain the dense, hash-verified private archive and
evaluate all profiles.

## Measured wide-universe rollout

The three public SEC-float memberships were exercised in order on this Windows
machine with `scripts/measure_wide_freeze.py`, the local verified SEC bulk
cache, SEC-only prices, and separate scratch output roots. The residual column
reports the aggregate across all eleven profiles, followed by the per-profile
mean in parentheses.

| N | Snapshot build | Shared matrix | Profile residual | Total | Peak RSS | Panel disk | Graded fraction | Unresolved CIKs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 250 | 154.497 s | 56.609 s | 166.896 s (15.172 s mean) | 381.875 s | 534,245,376 B | 165,042 B | 95.467% | 0 |
| 500 | 185.465 s | 70.709 s | 299.525 s (27.230 s mean) | 558.708 s | 842,231,808 B | 232,870 B | 94.867% | 0 |
| 1,000 | 421.522 s | 138.020 s | 690.361 s (62.760 s mean) | 1,253.896 s | 1,456,394,240 B | 391,314 B | 94.078% | 0 |

N=1,000 completed in 20.90 minutes, comfortably below the 100-minute local
acceptance ceiling and the workflow's 300-minute timeout. Each stage wrote
nine panels and returned success while explicitly refusing `low_volatility`
and `momentum`: SEC-only prices do not contain the dense daily histories those
profiles require. The graded fraction is calculated over the nine panels that
passed their safety gates; the two absent panels are not silently counted as
graded. Scratch panels and timing JSON stay outside git under `C:/tmp`.

For the same-date `all_weather` panels, widening from 82 to 1,000 names changed
mean coverage from 0.763932 to 0.710718, median coverage from 0.784050 to
0.730159, and graded fraction from 95.122% (78/82) to 94.100% (941/1,000).
The modest 1.022-percentage-point graded-fraction decline does not justify
retuning `MIN_COVERAGE_TO_GRADE`; it remains 0.35 so M5 does not confound a
universe change with a gate change. The exact diagnostic is preserved outside
git at `C:/tmp/m5-coverage.json`.

## Measured 82-name refactor speedup

On this Windows machine, the same cache-only command (`stock-grader freeze
--all-profiles --universe config/universe_default.txt --no-network
--price-provider sec --asof 2026-07-30`) took 1,148.819 seconds before shared
multi-profile computation and 200.401 seconds after it. The after time is
17.4% of the before time (5.73x faster; an 82.6% reduction), below M5's 25%
acceptance ceiling. Both commands attempted all eleven profiles, returned 2,
and refused the same seven profiles because the deliberately cache-only SEC
price source lacked their defining pillars; that makes the computation being
timed like-for-like.

## Runtime escape hatch

If the measured 1,000-name run stops fitting the 300-minute cloud budget, the
safe escape hatch is a `--profiles a,b,c` freeze selector and three workflow
jobs of roughly four profiles each. Every job must still load and score the
entire universe. Sharding by ticker is invalid: scoring is cross-sectional, so
each ticker shard would normalize against a different peer set and produce
incomparable percentiles, letters, and universe fingerprints.


## Statistical accounting

The target of eleven profile panels per month declares eleven research arms,
even when the SEC-only price gap prevents some files from being written. Every
eventual evaluation must remain in the lifetime trial ledger. The
broader cross-section nevertheless buys substantially more power: the
repository's null simulation estimates the expected maximum spurious IC over
105 metrics at 0.306 for N=82 and 0.122 for N=500.

This infrastructure is a research screen, not investment advice. A frozen
grade or later backtest verdict is evidence about a pre-registered method, not
a prediction or recommendation.
