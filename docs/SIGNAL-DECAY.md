# Signal decay — measuring how long the score's information lives

`stock-grader decay --vault <Stock-Vault clone> --profile all_weather
--primary-horizon 21 --allow-unverified-panel --ledger <path> --format md`

The ecosystem assumes a monthly holding period because the freeze runs monthly.
Nobody had measured whether the score's information lives 5, 21, 63 or 126
trading days. `decay` builds one backtest-shaped panel per horizon from the
same frozen scores and reports the rank-IC curve, its half-life (when the fit
supports one), and IC/√day — so the holding period is chosen from evidence.

## Why one panel per horizon

`backtest.py` raises `"signal_date … mixes return windows"` when one signal
date carries two windows, and its duplicate-(signal_date, ticker) guard makes
stacking horizons in one file structurally impossible. The sweep is therefore N
physically separate panels; a test proves the constraint is forced, not chosen.

## Overlap honesty: the dual view

With monthly signals a 63-day window overlaps its neighbours. The MEAN of a
per-date cross-sectional IC stays unbiased under overlap — only its standard
error is understated — so the descriptive view uses every signal date. The
inference view is the Jegadeesh–Titman average over ALL `overlap` disjoint
offset subsamples: each statistic (IC IR, net spread, Sharpe) is computed on
each non-overlapping subsample and equal-weight averaged, which uses every
period exactly once, is deterministic and order-independent, and replaces the
old arbitrary fixed offset 0. Never search offsets — averaging all of them is
the point.

The offsets are built from the same overlapping returns, so they are strongly
correlated: the average stabilizes the point estimate but must NEVER be read
as tightening a CI. Significance and the gate therefore use the most
conservative offset (a failing offset first, then lowest DSR, then lowest CI
floor) — the gate passes only when no phase choice could have flipped it. A
nonempty offset that cannot be evaluated fails the whole horizon; averaging
only the offsets that happen to work would re-introduce phase dependence.
`eff.` in the report is the mean periods per offset. When `overlap == 1` the
single subsample is the whole panel, exactly as before.

## Half-life: weighted fit with an honest interval

`fit_half_life` is an inverse-variance-weighted OLS of ln(mean IC) on horizon:
each point is weighted by the precision implied by its per-horizon
moving-block-bootstrap IC interval (delta method into log space), so one noisy
long-horizon point cannot steer the fit. It reports a 95% half-life interval
propagated from the slope's known-variance standard error. Every refusal is
unchanged — fewer than 3 positive-IC horizons, non-decaying IC, poor fit
(weighted R² < 0.5) — and two more honesty rules apply: when any usable
horizon lacks an IC interval the fit falls back to unweighted and reports no
half-life interval rather than inventing precision, and when the decay rate is
not distinguishable from zero at 95% the interval is unbounded above and
therefore withheld (the point estimate and the note still say so).

## The trial charge

Every horizon is one ledger trial on ONE shared, order-independent denominator
computed before any append. Only the pre-declared `--primary-horizon` may pass
the gate; every other horizon is recorded `EXPLORATORY` with `gate_passed:
false` unconditionally — a sweep must never promote its own argmax. The E[max]
deflation assumes independent trials; horizons of one score are highly
correlated, so it over-deflates. The remedy is the pre-declared primary, never
a private discount.

## Data limits (why --allow-unverified-panel is mandatory)

Price-only unadjusted returns from the vault archive (no dividends, no
delisting proceeds), today's surviving universe, no filing cutoff: four of the
evaluator's five contract items fail by construction. `cik` passes. A missing
exit price is dropped and counted (or imputed via `--delisting-return`, still
counted); split-shaped jumps are screened and counted.

Outputs land in `signal_decay/<profile>/<archive_through>/` — **gitignored**:
they embed vault-derived forward returns (Massive personal-use licence) and
must never reach this public repository.

## Retro panels

`stock-grader freeze --asof <D> --pit --out retro_scores` can manufacture
history back to the archive start, and `decay --frozen-dir retro_scores`
consumes it — but retro panels are survivorship-biased (today's universe) and
their PIT vintage is compromised by the SEC cache's 24h TTL. Never write them
into `frozen_scores/`; `panel_origin` records `retro_backfilled` in every
artifact and report.

## Scheduling

Deliberately NOT a GitHub Action: the vault is private and local-clone-only by
design. Run `scripts/monthly_decay.sh` locally (cron or by hand); it refuses to
run on a stale vault (>4 days) or a dead freeze clock (>40 days).
