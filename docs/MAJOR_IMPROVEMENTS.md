# Major improvements — implementation handoff

This is the execution plan for the five large improvements to the stock-research ecosystem, written so an
agent with **no memory of the conversation that produced it** can carry them out unaided.

Each milestone lives in its own file under [`majors/`](majors/). Every factual claim in those files was
verified by reading the actual code or opening the actual data file, and carries a `path:line` citation.
Where a milestone says "verified", it means someone looked — not that someone assumed.

**Read [`majors/ORIENTATION.md`](majors/ORIENTATION.md) before anything else.** It covers the three repos,
the artifact contract between them, the licensing split, every scheduled workflow, and the bug classes that
have broken this system before.

## Current state (2026-07-30)

All three repos are clean, green, and pushed: Stock-Grader `5290a2f` (582 tests), Stock-Vault `b407524`
(73 tests), Stock-Data `15b0ad2` (38 tests).

Landed today, after these specs were researched — every milestone file carries a status banner saying the
same thing, because several of them were written while this work was still in flight:

- **Multi-profile freeze.** `freeze --all-profiles` grades one set of snapshots under all 11 profiles and
  writes `frozen_scores/<profile>/YYYY-MM-DD.parquet`. The first cloud run committed **nine** panels for
  2026-07-30; `momentum` and `low_volatility` refused, which is the unwired-price-provider problem below.
- **The paper trader is live and holding positions** — 10 orders placed on 2026-07-30. Panel loading is
  profile-aware, order failures no longer abort a rebalance, and every degenerate input (missing equity,
  empty panel, stale panel) trips an interlock instead of trading.
- **Benchmark and fill journaling.** The journal now emits four record kinds: `rebalance`, `snapshot`,
  `benchmark` (SPY buy-and-hold and equal-weight counterfactuals from `BENCHMARK_START_DATE`), and `fill`
  (with `drift_bps`, `reference_close`, `reference_date` — named drift rather than slippage because the free
  EOD archive lags two sessions).

If a milestone file and the code disagree, the code wins. Re-read before editing: every line number in those
specs predates this work.

## The milestones

| | Milestone | What it buys | Effort |
|---|---|---|---|
| **M5** | [Widen the universe to 500–1000 names](majors/M5-wide-universe.md) | Statistical power. Top-10-of-82 is noise-dominated; cross-sectional power grows roughly with √breadth. | large |
| **M1** | [Forward panel builder + scheduled backtest](majors/M1-forward-panel-and-backtest.md) | **The linchpin.** Nothing currently joins frozen panels to realized returns, so evidence accrues un-evaluated. | large |
| **M3** | [Shadow paper arms for all 11 profiles](majors/M3-shadow-arms.md) | Eleven strategy arms instead of one, replayable retroactively from frozen panels. | large |
| **M4** | [Auxiliary signal panels from vault collectors](majors/M4-auxiliary-signals.md) | Turns borrow/short-interest/recs/ETF archives into *evaluable signals* rather than archives. | large |
| **M2** | [Signal-decay measurement](majors/M2-signal-decay.md) | Learn the score's natural holding period instead of assuming monthly is right. | large |

## Do them in this order, and here is why

The ordering is driven by one asymmetry: **the frozen record is a clock, and clocks cannot be rewound.**

Anything that changes *what gets frozen* must happen as early as possible, because a month frozen at 82
names is permanently a month of 82 names. Anything that merely *analyses* what was frozen can be built
whenever and applied retroactively to the whole history.

1. **M5 (widen the universe) — first, and the urgency is real.** Widening changes peer groups, the letter
   floor, and sector-neutral scoring, which means panels before and after the change are not directly
   comparable: there is a regime break in the frozen record wherever it lands. Exactly **one** frozen panel
   exists today. Doing this now costs one month of comparability; doing it in a year costs a year of it.
2. **M1 (close the loop) — second, build it while panels mature.** It is the only milestone that answers
   "does any of this work?", but it needs ≥3 matured panels before it produces a meaningful verdict. Build
   the machinery now so it starts evaluating the moment there is something to evaluate.
3. **M3 (shadow arms)** — replays retroactively from frozen panels, so it loses nothing by waiting; it
   depends on the multi-profile freeze that just landed.
4. **M4 (auxiliary signals)** — the raw collector archives are already accruing, so distillation is
   retroactive. No urgency, high eventual value.
5. **M2 (signal decay)** — needs the longest matured history (126-day horizons), so it is genuinely last.

## Before starting any milestone: a 20-minute fix worth doing first

`research_ledger.jsonl` currently holds **12 records that are not real hypotheses** — every one is
`experiment: "backtest:panel.csv"` with an identical `per_period_sharpe` of 2.8284271247461894, appended by
CLI tests running against synthetic panels.

This matters because the deflated Sharpe ratio benchmarks a new result against the *dispersion* of prior
trial Sharpes (`significance.py:170-183`). Twelve identical synthetic trials distort that denominator, and
the first real forward verdict would be computed against a fictional trial history.

The ledger is append-only and hash-chained on purpose, so **do not delete those lines.** The correct fix is
retraction records — append records that mark the synthetic trials as retracted, and teach the trial-sharpe
collector to skip retracted experiments. This is specified in detail as Step 9 of
[M1](majors/M1-forward-panel-and-backtest.md), and it should be done before any real trial is recorded.
Also make the test suite write to a temporary ledger so it stops polluting the real one.

## Also do early: wire the vault price archive into the grader CLI

**Two of the eleven scoring lenses are currently blind, and the data they need already exists.**

Measured, not theorised: the first all-profiles cloud freeze
([run 30557005303](https://github.com/TylerJForstrom/Stock-Grader/actions/runs/30557005303), 2026-07-30)
wrote nine panels and refused two — `momentum` and `low_volatility` each graded **0 of 82** names. They need
a dense daily price series, and the freeze runs with SEC data only.

Stock-Vault holds exactly that series: `data/market_eod`, ~501 trading days × ~12,400 tickers, collected
daily. `VaultDataSource` and `VaultPriceProvider` in `src/stock_grader/data/vault.py` already read and
hash-verify it. But `grep -n vault src/stock_grader/cli.py` returns **nothing** — the adapter was built and
never connected, so `--price-provider` has no `vault` choice and no command can reach the archive.

The fix is small and unlocks disproportionate value (two whole style lenses, plus denser prices for every
other profile):

1. Add `vault` to the `--price-provider` choices and a `--vault-dir` argument, routing to the existing
   `VaultPriceProvider`. Register both on the subparsers via `parents=[shared]`.
2. In `monthly-freeze.yml`, check out Stock-Vault with a `VAULT_REPO_TOKEN` secret (a fine-grained PAT with
   contents:read on the private vault — the same secret M1 requires) and pass `--price-provider vault
   --vault-dir <checkout>/data`.
3. Expect the alarm policy to fire once, correctly: after this lands, `momentum` and `low_volatility` should
   start grading. If they later refuse *again*, that is now a regression and the run will go red — which is
   the desired behaviour.

Until this is done, treat any conclusion about momentum or low-volatility as unsupported: those arms have no
evidence at all, not weak evidence.

## Ground rules (non-negotiable)

These are distilled from failures this ecosystem has already had. `majors/ORIENTATION.md` has the full list
with context; these are the ones that bite hardest:

- **Schemas are additive only.** Never wrap, rename, or restructure an existing JSON record or parquet
  column. Consumers hard-refuse unknown schema versions, and two separate outages here came from "improving"
  an envelope.
- **argparse placement kills crons.** Subcommand-level flags need `parents=[shared]`. A top-level-only
  argument placed after a subcommand makes every scheduled run exit 2 — this silently killed an entire
  repo's clocks once and was only caught days later.
- **Never fail open on a numeric field from a broker or a data feed.** A missing `equity` field defaulting
  to 0.0 nearly liquidated the paper account. Absent or unparseable means raise, not "assume zero".
- **Verify every workflow dispatch.** After changing any `.github/workflows/*.yml`, actually dispatch it and
  confirm with `gh run list` that it went green. Reading the YAML is not verification.
- **Point-in-time discipline is the whole product.** Any value used on a signal date must have been
  *knowable* on that date. If you cannot prove it, attest `false` rather than attesting optimistically — the
  backtester's contract exists to make honesty cheaper than self-deception.
- **PowerShell 5.1 quirks (this is a Windows machine).** Never pipe a secret to a native command's stdin —
  PowerShell appends CRLF and corrupts it (this produced days of mysterious 401s). Don't redirect a native
  command's stderr with `2>$null` under strict mode.

## How to work through a milestone

- **One milestone per branch.** They touch overlapping files; running two at once produces conflicts that
  are expensive to untangle.
- **Every behavior change gets a regression test that fails on the old code.** Verify this by actually
  restoring the old file (`git show HEAD:path`) and watching the new test fail — a test that passes both
  before and after is not a regression test.
- **Run the full suite before reporting done.** Stock-Grader's takes about 12 minutes (574 tests as of
  2026-07-30); Stock-Vault's and Stock-Data's take seconds. Also run `python -m ruff check .` in each repo
  you touched.
- **Report honestly.** If an acceptance criterion cannot be met — say, because a data dependency does not
  exist yet — say so plainly and explain what is blocked. A milestone reported complete with a quietly
  skipped criterion is worse than one reported blocked.

## The prompt to hand to another agent

Copy this verbatim, substituting the milestone you want. It is written to be self-contained.

```text
You are implementing one milestone of a planned improvement to a stock-research ecosystem on a Windows
machine. Three git repositories, all already cloned:

  C:/Users/tforstrom/Desktop/Stock-Grader   scoring engine, backtester, significance/ledger, frozen panels
  C:/Users/tforstrom/Desktop/Stock-Vault    PRIVATE collectors + the Alpaca paper trader (never make public)
  C:/Users/tforstrom/Desktop/Stock-Data     PUBLIC point-in-time data foundry

READ FIRST, IN THIS ORDER:
  1. Stock-Grader/docs/MAJOR_IMPROVEMENTS.md          (the index: ordering, ground rules, how to work)
  2. Stock-Grader/docs/majors/ORIENTATION.md          (the ecosystem: contracts, DAG, licensing, clocks)
  3. Stock-Grader/docs/majors/<MILESTONE FILE>.md     (your milestone: ground truth, steps, acceptance)
  4. Stock-Grader/docs/HANDOFF.md                     (working rules and known bug classes)

YOUR TASK: implement <MILESTONE ID> exactly as specified in its milestone file, in order, start to finish.

RULES:
- The milestone file's "Verified ground truth" section was confirmed against the real code. If you find the
  code contradicts it, the CODE WINS: adapt, and report the discrepancy in your final summary.
- Schemas are additive only. Never rename or restructure an existing JSON record or parquet column.
- Never fail open on a numeric field from a broker or data feed: absent or unparseable must raise.
- argparse: subcommand flags need parents=[shared]. A misplaced flag makes every scheduled run exit 2.
- Every behavior change needs a regression test that FAILS on the old code. Prove it: restore the old file
  with `git show HEAD:<path>`, confirm the new test fails, restore your version.
- Do not delete or edit lines in research_ledger.jsonl. It is append-only and hash-chained; corrections are
  appended as new records.
- After changing any .github/workflows/*.yml, dispatch it (`gh workflow run <name> --repo <owner/repo>`) and
  confirm it went green with `gh run list`. Reading the YAML is not verification.
- Work on a branch. Do not push to main. Do not commit secrets or any file under a private data/ directory
  unless the milestone explicitly says to.

WHEN DONE, report:
  1. Each acceptance criterion from the milestone file, marked met or not met, with the evidence.
  2. The exact `python -m pytest -q` and `python -m ruff check .` summary lines for every repo you touched.
  3. Any discrepancy you found between the milestone file and the actual code.
  4. Anything you could not complete and precisely what blocks it.
Do not report the milestone complete unless every acceptance criterion is genuinely met.
```

Substitute one of: `M5-wide-universe`, `M1-forward-panel-and-backtest`, `M3-shadow-arms`,
`M4-auxiliary-signals`, `M2-signal-decay`.

## Not investment advice

Everything here is research infrastructure. A passing backtest is a hypothesis that survived one honest
test, not a recommendation.
