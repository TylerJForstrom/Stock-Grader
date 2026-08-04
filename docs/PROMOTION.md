# PROMOTION-POLICY v1 — the pre-registered arm-promotion gate

Version string: **`promotion-policy-v1`**. This document is the versioned
promotion policy that Stock-Data `ECOSYSTEM.md` (decision log 2026-08-04 and
rule 8, the money boundary) references by name. Its exact bytes are bound by
sha256 into the append-only `research_ledger.jsonl` via a `ledger:promotion`
policy-declaration record (`stock-grader promotion-declare`). **Amendment is
a NEW version declared by a NEW record — this file is never edited in place
once declared, and superseded declarations stay in the chain.** The CLI
enforces both: a changed document under the same version string is refused,
and every stage transition is validated against the policy *as declared in
the ledger*, not against code constants.

Written 2026-08-04, before any matured forward window exists. That timing is
the point: promotion criteria can only be pre-registered unimpeachably while
no comparative evidence exists to fit them to. Eleven forward curves are
accruing (9 profile shadow arms + 2 controls); the first honest comparative
read arrives ~late 2026. After that, any "policy" would be selection dressed
as method.

## The four stages, plus the rung above them

```
exploratory  ->  declared_trial  ->  shadow_arm  ->  paper_default      [live_money]
   (0)               (1)               (2)              (3)          exists, UNREACHABLE
```

| stage | meaning | evidence home |
|---|---|---|
| `exploratory` | Hypothesis under construction: scratch-ledger runs, vault signal panels, notebook work. Nothing is charged to the real trial denominator. | Scratch ledgers; Stock-Vault `docs/SIGNAL-PANELS.md`; vault decision journal |
| `declared_trial` | The spec is frozen and pre-registered (`ledger-declare` for public subjects; the vault decision journal for license-walled ones) and scheduled evaluations charge ONE trial on the real denominator. | `research_ledger.jsonl` (public subjects) / vault decision journal (license-walled) |
| `shadow_arm` | A pre-registered forward instrument replays the spec daily under the shadow engine's identity contract (`sim-v1-nextclose-5bps`), accruing a journaled curve. | Stock-Vault `data/shadow_journal/` |
| `paper_default` | The ONLY action of the first passed gate: the Alpaca **paper** account's `DEFAULT_PROFILE` (Stock-Vault `paper.py`) switches to the promoted profile. Nothing else changes. | Stock-Vault `data/paper_journal/` |
| `live_money` | Exists so the ladder is honest about where it points. **Unreachable under v1** (`live_money_reachable: false` in the declared policy core — the CLI refuses the transition). See "Un-stopping the money boundary". |

`retired` is a terminal state outside the ladder (see "Demotion and
retirement"). Stages are climbed one rung at a time, each climb a
`ledger:promotion` stage-transition record citing evidence hashes. Demotions
may fall any number of rungs.

Subjects that predate this policy (the nine profile arms already journaling)
hold no assumed stage: before any further promotion, their history must be
recorded as explicitly-labeled retroactive transitions (evidence: the
pre-registration record hash, the arm's journaled identity) — the ladder is
walked in the ledger, never presumed.

## Gate 1: exploratory -> declared_trial

**No real-ledger trial before the panel's evaluable periods give the
significance gate meaningful power at the minimum IC worth trading.** The
minimum IC worth trading is declared here as **rank IC 0.03**: under the
evaluator's cost model (10 bps per unit turnover; the shadow engine charges 5
bps per side) a thinner edge at monthly rebalancing is consumed by costs, and
0.03 is also the smallest planted IC the gate detects with >= 50% probability
at any horizon the calibration grid measured.

All of the following, citing `docs/calibration/power_table_2026-08-03.md`:

1. **Structural floor.** The gate is structurally closed below 11 evaluable
   periods — `block_bootstrap_sharpe_ci` returns (0, 0) under 11 periods, so
   `CI low > 0` can never hold ("Gate anatomy": every 3- and 6-month row
   shows gate rate 0.00 at every planted IC). Declaring a trial the gate
   cannot physically pass is charging a trial for nothing.
2. **Power floor.** The (periods, cross-section) point must reach a
   power-table row with >= 0.50 detection at the minimum IC worth trading:
   for ~1000-name panels that is **12 monthly periods** (row: planted IC
   0.03, 12 mo, universe 1000 -> 0.65 power; 36 mo -> 1.00); for ~250-name
   panels IC 0.03 never reaches 0.50 on the grid (0.15 at 12 mo, 0.45 at
   36 mo), so a ~250-name subject binds to **IC 0.05 at 12 periods** (row:
   0.65) and must say so in its declaration — its minimum detectable edge is
   larger.
3. **Honest-cell discipline.** Tabulated rates carry binomial SE up to ~0.11
   (the table's own provenance note): thresholds here bind to the table's
   STRUCTURAL facts (closed < 11 periods; `INSUFFICIENT SAMPLE` verdict
   < 30 periods; false-positive rate 0.00 at planted IC 0 in every null
   cell) and to trend-consistent rows, never to a single noisy cell.
4. **Cadence honesty.** The grid is calibrated at MONTHLY cadence. A signal
   whose true cadence is daily (both borrow signals — Stock-Vault
   `docs/SIGNAL-PANELS.md`: "the spec's 12 periods/year label understates
   the real cadence; pass the true cadence to the evaluator") must declare
   its true `--periods-per-year`, and because the grid never simulated daily
   cadence, daily-cadence subjects additionally satisfy a **calendar floor
   of 12 months of signal dates** before declaration. Daily periods are not
   monthly periods and buying power by mislabeling them is refused.
5. **The declaration act.** Public subjects: `stock-grader ledger-declare`
   (spec hash + schedule; scheduled re-evaluations collapse to one trial).
   License-walled subjects: the same spec hash + schedule appended to the
   vault decision journal, with only the stage-transition record (hashes,
   stage names) appearing publicly.

Below 30 evaluable periods every verdict additionally reads `INSUFFICIENT
SAMPLE`: a pass between 11 and 30 periods is provisional and must repeat at
the next scheduled look before it counts as a pass anywhere in this policy.

## Gate 2: declared_trial -> shadow_arm

A shadow arm is an instrument, not a reward — the bar is operational
integrity, not performance:

1. The declared spec has at least two scheduled evaluations already recorded
   (the trial demonstrably runs on its declared schedule).
2. Frozen panels exist for the subject's profile (the arm needs a panel
   schedule to replay), written with the standard sibling
   `manifest.json` the vault verifies.
3. The arm's identity contract is pinned before its first session
   (`SIM_VERSION` / `RULES_VERSION` fail-closed identity, `arm.json`).
4. **Multiplicity is charged at entry**: the arm-inference family is fixed at
   spec time (see below). A new arm is *reported but not scored* until a new
   arm-inference spec version registers it and restates the family charge.
   Registering the arm is part of this gate, not an afterthought.

## Gate 3: shadow_arm -> paper_default

BOTH evidence streams, each at its own declared floor, and the action on
passing is ONLY the `DEFAULT_PROFILE` switch of the Alpaca paper account:

**Panel-level (the significance gate).** The subject's pre-registered
monthly evaluation passes (`DSR >= 0.95 AND bootstrap CI low > 0`) at two
consecutive scheduled looks with >= 12 matured monthly periods — and once
past the 30-period `INSUFFICIENT SAMPLE` line, a pass no longer needs the
provisional repeat. Per the power table's own over-reading warning, a pass
is checked against the table's power at the realized IC before it is
celebrated: an underpowered gate mostly passes lucky draws of large signals.

**Arm-level (the vault scoreboard's declared test).** Cited precisely as the
spec instructs: *arm-inference-v1 (Stock-Vault `docs/ARM-INFERENCE.md`,
sha256 `ed70b7ff6779fe880966d92ad5f9a23744f985b92d668bf8fe7a162b505e347d`,
mirrored verbatim in every `data/reports/arms_scoreboard/*.json` header as
`INFERENCE_SPEC` in `src/stock_vault/scoreboard.py`)*:

- statistic: the arm's final-session equity excess over its OWN profile's
  SPY-hold cost twin (identical fill/cost machinery, identical panel
  schedule);
- null: K = 199 RNG-free seeded rank-sampling draws per profile (namespaces
  `control-random10-v1-e001`..`control-random10-v1-e199`, ensemble
  `null-ens-v1-k199`), sized by `paper.target_portfolio`, filled through
  `shadow._Arm.step`, same twin subtracted;
- p-value: one-sided rank, `(1 + #{null_excess >= arm_excess}) / (K + 1)`;
- threshold: family alpha 0.05 one-sided across the 9 registered arms,
  Bonferroni-charged — per-arm **0.05/9 ~= 0.00556**, with K = 199 chosen so
  the minimum attainable p-value 1/200 = 0.005 clears it;
- look schedule: the `shadow-arms.yml` cron `41 3 * * 2-6` plus explicit
  `workflow_dispatch` runs; every dated scoreboard artifact is one disclosed
  look and no other comparative read of arm evidence is sanctioned.

The promotion criterion: the arm's p-value is at or below the corrected
threshold at **two consecutive scheduled looks**, with the arm's journal
spanning at least **252 sessions (~12 months)** — the scoreboard reports
percentiles from day one, but this policy's sample floor is what makes them
decision-grade.

## Un-stopping the money boundary: paper_default -> live_money

Unreachable under v1, by declaration (`live_money_reachable: false`) and by
CLI refusal. The rung opens only when ALL of:

1. the declared Gate 3 (panel-level AND arm-level) **passes twice on
   schedule** *after* the paper-default promotion — sustained, not a spike;
2. a NEW policy version (promotion-policy-v2 or later) is declared into the
   ledger with `live_money_reachable: true`, superseding — not editing —
   this one;
3. a NEW dated decision-log entry lands in Stock-Data `ECOSYSTEM.md` per
   rule 8: no code, configuration, or workflow change may un-stop the money
   boundary silently.

## The 9-arm multiplicity charge

The registered family is fixed at arm-inference-v1 spec time: `all_weather`,
`deep_value`, `dividend_growth`, `dividend_income`, `garp`, `growth`,
`quality`, `turnaround`, `value` — nine arms, Bonferroni per-arm threshold
0.05/9 ~= 0.00556. When the eleven curves become readable, the tempting move
is to compare nine curves and favor the best — an unregistered 9-way
selection, exactly the bias DSR corrects at the panel level. This charge is
the arm-level correction, declared before any curve is readable. The family
never shrinks: **retiring an arm does not un-charge it** (the looks at it
happened), and adding an arm requires a new arm-inference spec version that
restates the whole charge.

## Demotion and retirement

Demotion is a stage-transition downward (any number of rungs), with the
reason and evidence hashes recorded. Pre-declared triggers:

- **Performance**: at two consecutive scheduled looks the `paper_default`
  arm's excess versus its twin sits at or below the null ensemble's 50th
  percentile — the promoted selection is doing no better than the median
  random draw — demote to `shadow_arm` and revert `DEFAULT_PROFILE`.
- **Panel-level reversal**: with >= 30 matured periods, the subject's
  scheduled evaluation shows `CI low <= 0` at two consecutive looks —
  demote from `paper_default`.
- **Integrity**: a persistent refusal state (scoreboard determinism refusal,
  identity-contract breach, frozen-panel manifest refusal) across 5
  consecutive scheduled looks — demote one rung until cured. An instrument
  that cannot attest may not hold a rung.

`retired` is terminal: taken when a spec is retracted, its panel supply ends,
or the hypothesis is abandoned. A retired subject never re-enters the ladder;
revival means a new spec, a new subject hash, a new exploratory start, and a
new trial charged. Retirement never shrinks the multiplicity family (above).

## Concrete decision points: September / October 2026

The first real decisions this policy governs arrive on the vault
`signal-panels.yml` calendar (Stock-Vault `docs/SIGNAL-PANELS.md`, "Earliest
evaluable"). Every look below is a scheduled, disclosed decision recorded in
the vault decision journal (numeric evidence stays vault-side; a public
stage-transition record appears only if a stage actually changes). Declaring
these looks now, with their expected verdicts, is what makes the waiting
disclosed peeking rather than silent peeking:

| decision point | subject | expected verdict | what would change it |
|---|---|---|---|
| 2026-09-03 build | `borrow_fee_level` (IB-derived; license-walled) | **remain exploratory** — ~24 evaluable periods by this build, but they are DAILY periods spanning ~5 weeks; Gate 1's calendar floor (12 months of signal dates, cadence honesty rule) binds until ~2027-07 | nothing at this look can; the look verifies data continuity and true-cadence declaration |
| 2026-09-03 build | `borrow_fee_change` (IB-derived; license-walled) | **remain exploratory** — first evaluable period lands in this build | same as above; floor ~2027-08 |
| 2026-10-03 build | `rec_consensus_delta` (Finnhub-derived; license-walled) | **remain exploratory** — the second signal date is UNCONFIRMED (the 2026-08 snapshot carried no August row; SIGNAL-PANELS.md note); at best 1 period by this build | the look confirms whether the 2026-09-02 snapshot carried a September row; a meaningful IC needs ~a year of monthly periods from whenever the second signal date arrives |
| 2026-10-03 build | `etf_share_change` (SSGA-derived; license-walled) | **remain exploratory** — first period (2026-08-31 signal, 2026-09-30 successor) lands in this build: 1 period vs a 12-period floor | nothing at this look can; earliest Gate 1 ~2027-09 |

None of these four can reach `declared_trial` before mid-2027 by this
policy's own floors. That is the policy working: the significance gate is
structurally closed below 11 periods, so a "trial" declared earlier would be
theater charged to the denominator.

## The public/vault split (licensing wall)

Promotion decisions about license-walled vault signals (Finnhub-, FINRA-,
IB-, SSGA-, and Massive-derived) must not put derived results in this public
repo. The split, by construction:

**Public — `research_ledger.jsonl` (this repo).** Policy declarations
(version string + policy-document sha256) and stage-transition records,
expressed ONLY as: spec hashes, stage names, the policy version, evidence-
record integrity hashes, and the vault journal's locator + chain-head hash.
A sha256 is not a derived result; a signal's NAME and its stage are not
derived results. No IC, Sharpe, p-value, return, percentile, or any other
number derived from licensed data ever appears in a `ledger:promotion`
record. These records carry no metrics, so they can never enter the trial
denominator either.

**Private — Stock-Vault `data/decision_journal/decisions.jsonl.gz`.** The
append-only decision journal (`stock_vault.decisions`) mirrors the vault
journal discipline (byte-stable gzip, mtime=0, no FNAME) and this ledger's
chain discipline (per-record integrity sha256 + `prev_sha256` link from a
genesis sentinel). Every scheduled decision look — including every "hold" —
appends one record carrying the FULL numeric evidence (periods, ICs,
p-values, percentiles, verdict). The public transition record cites the
journal record's `integrity_sha256` and the journal's head hash at decision
time: with vault access every cited hash recomputes; without it, the public
chain still proves what was decided, when, under which policy version.

**Public subjects** (grader profile panels, built from public frozen scores)
may cite public ledger records — backtest results, preregistrations —
directly as evidence hashes; their numbers were never behind the wall.

## Record mechanics

- Record kind: `ledger:promotion` (`stock_grader.research_manifest`), riding
  the existing schema additively exactly like `ledger:retraction` and
  `ledger:preregistration` — self-hash in `symbols[0]`, canonical
  declaration JSON in `leakage_controls`, **no metrics** (never in the trial
  denominator), hash-chained like every other line.
- CLI: `stock-grader promotion-declare` — policy mode declares this document
  by sha256 (idempotent per version+hash; changed bytes under the same
  version are refused); transition mode refuses a broken chain, an
  undeclared policy, a drifted policy document, a wrong `from_stage`, a
  skipped rung, an evidence-free promotion, and the live-money rung under
  v1.
- Verification: the standard checker (`verify_chain` +
  `summarize_manifest`), unchanged — promotion records are ordinary chained
  lines.
