# HANDOFF — remaining fixes, in order (2026-07-29)

For the next agent (Codex or Claude). Read AGENTS.md and docs/REVIEW_FEEDBACK.md
first; this doc is the work queue. The goal is "implement all fixes in the
correct order" from the ecosystem gap analysis. Tier 1 items 1–3 are DONE
(VaultDataSource `src/stock_grader/data/vault.py`, significance/ledger wiring in
`cmd_backtest`, `freeze` command + monthly workflow). Continue from item 4.

## Working rules (non-negotiable, learned the hard way this week)

1. One coherent chunk per commit; full suite green before AND after
   (`pip install -e ".[dev]"` first — two subprocess tests import the live tree).
2. **Never wrap a documented JSON schema in an envelope** — add keys additively.
   This mistake was made twice this week; both times tests caught it.
3. CLI args that workflows pass subcommand-first MUST be registered on the
   subparser (`parents=[shared]` pattern) — the top-level-only variant silently
   killed every scheduled run in two repos ("DOA workflow" bug class).
4. PowerShell 5.1: never `2>$null` a native command under strict mode; never
   pipe secrets to stdin (appends CRLF — corrupted every GitHub secret once).
5. After dispatching any workflow, VERIFY the run completed (`gh run list`) —
   a dispatched-but-dead run burned a day of the PIT archive.
6. Suite runtime is ~20 min. Batch: targeted tests per chunk, full suite per
   2–3 chunks in background (`run_in_background`), fix forward if red.

## The queue (in execution order)

### 4. Ticker canonicalization spec  (~half day)
Four symbologies exist: SEC `BRK-B`, Polygon `BRK.B`, IB `BRK B`, TickerPulse
`BRK.B`. `ticker_variants()` in `src/stock_grader/data/symbols.py` bridges
dash/dot; VaultDataSource.borrow_fee adds the space form ad hoc.
- Write the spec into Stock-Data's ECOSYSTEM.md: canonical = SEC dash form.
- Extend `ticker_variants` with the space form; make every adapter boundary
  (foundry.py, vault.py, sec_prices.py) canonicalize on read.
- Copy the ~20-line helper into Stock-Data and Stock-Vault (`no cross-repo
  imports` rule) and apply at their write paths for NEW data only.

### 5. events.jsonl into the manifest contract + universe(asof)  (~half day)
`Stock-Data/src/stock_data/symbols.py:216` writes events.jsonl OUTSIDE any
manifest; FoundryDataSource can never read it (foundry.py requires manifest
listing). Fix: give it its own dataset dir `data/symbols/events/` with
manifest.json (bump nothing — additive), then add
`FoundryDataSource.universe(asof=...)` that replays added/removed events
backward from current. Enables PIT ticker→CIK mapping for the panel builder.

### 6. Delisted-archive CIK linkage  (~1 day, AFTER the events sweep has data)
`Stock-Vault/src/stock_vault/delisted.py:74-82` cohort_index rows lack CIK.
One-time resolution pass: match symbol+delist-date against the events dataset's
Form 25/15 rows (CIK-keyed) + name matching as tiebreaker; write `cik` into
cohort_index.json. Check first that the weekly-events sweep succeeded
(`gh run list --repo TylerJForstrom/Stock-Data`).

### 7. Ops hardening bundle  (~half day, mostly YAML)
- Staleness self-checks in every scheduled workflow: fail the run when the
  newest relevant artifact is older than its cadence (borrow >3 days,
  market_eod weekday gap, recs >35 days, snapshot >2 days). GitHub emails the
  owner on scheduled-workflow failure — that IS the alerting; no external
  service needed.
- recs.py: port market_eod's 401/403 SystemExit; workflow fails on empty
  universe.txt or missing output file.
- market_eod cron default `--backfill-days 35` (was 5) so outages self-heal.
- Vault mirror: a Windows Task Scheduler job doing weekly
  `git -C Stock-Vault bundle create <backup-path>` (document in vault README;
  the machine-independence rule tolerates this because it is a BACKUP, not
  the primary).
- Vault yearly delisted top-up cron (`stock-vault harvest-delisted --years
  $(date +%Y)`, June 15th).
- FINRA collector: move invocation from Stock-Data (dead code there) into
  Stock-Vault's collectors.yml; delete the Stock-Data copy.
- SSGA collector: build it (pattern = borrow.py, ~1h) or delete its README row.

### 8. Methodology remainder  (~1 day)
- ~~`research.py` `_fmt`/unit vocabulary~~ DONE (2026-08-02): `dimensionless`
  unit renders `{:,.2f}x`; all 69 `unit="ratio"` registrations audited, ten
  reclassified (Sharpe/Sortino/Calmar/tail/variance ratios, ulcer index,
  risk-adjusted momentum, Amihud, price/SMA200, Graham number).
- ~~Defining-pillar gate~~ DONE (2026-08-02): `min_defining_pillar_coverage`
  (default 0.4) consults `PillarScore.coverage`. NOTE: this retired the legacy
  default config fingerprint `751441e6…` — panels frozen from 2026-09 carry
  `1790775d…` and are not comparable with earlier ones.
- ~~`_metric_evidence` fallback mixing~~ DONE (2026-08-02): target-side
  numbers come exclusively from `report.explain["metrics"]`; the matrix is
  rebuilt only for peer quartiles; reconciliation test added.
- Supervised guard: raise only when a supervised method is selected AND
  forward_returns is not None (currently blocks even the harmless case).
- `winsorized_z` (normalize.py): clamp at median±k·MAD when n<100 (the
  1st/99th quantile clamp is a no-op at these sizes).

### 9. Consumption layer  (~2 days)
- Run journal: append each run's JSON to `~/.stock-grader/runs/` keyed by
  asof+fingerprints; `stock-grader diff --since-last TICKER` refuses
  mismatched fingerprints, reports letter/score/pillar deltas AND the metric
  contributions that moved them. This finally makes `apply_hysteresis` +
  `previous_letters` (pipeline.py:797) reachable from the CLI.
- `ecosystem-status` command in Stock-Data: one table — every clock, last
  tick UTC, staleness vs cadence, via raw GitHub URLs + local vault clone.
- Dossier "What drove this grade": top-5 strengths/concerns in words from
  existing contribution data; collapse zero-contribution rows to a count.
- Provenance header (asof + fingerprint prefixes) in the two markdown
  renderers.

### 10. Deferred (needs data volume or a decision)
- ~~§6 panel builder~~ DONE (M1, 2026-08-02): `stock-grader build-panel` +
  `monthly-forward-backtest.yml`. Backtest calibration harness (planted-IC
  power table) is still open and belongs with it.
- Li-Mohanram mechanical forecasts (Stock-Data derived artifact; spec in
  docs/DATA-FOUNDRY.md).
- ~~Paper-trade bridge~~ DONE (M3, 2026-08-03): shadow arms in Stock-Vault
  replay `paper.target_portfolio` over every profile's frozen panels against
  the EOD archive; one journal per arm, byte-deterministic, calibrated against
  the real account's fills.
- §4 dossier additions (per-peer comp table, SBC, insider activity, red-flag
  display from the events dataset).
- Pre-registration record kind in research_manifest (declare grid before
  running; mark results "primary" only when spec hash matches).

## State of the clocks (verify before starting work)

| Clock | Where | Status at handoff |
|---|---|---|
| Daily PIT symbols | Stock-Data Actions | FIXED today; first cloud success 2026-07-29; verify tomorrow's scheduled run |
| Weekly 8-K events | Stock-Data Actions | First sweep dispatched 2026-07-29 — VERIFY it completed |
| Whole-market EOD | Stock-Vault Actions | 501 days backfilled (2024-07→2026-07); daily cron takes over |
| Borrow 2×daily | Stock-Vault Actions | Working (2 snapshots so far) |
| Monthly recs | Stock-Vault Actions | Untested in cloud — verify after the 2nd of the month |
| Sentiment 15-min | TickerPulse Actions | Working; first bucket-day archive file expected 2026-07-29 — verify |
| Monthly freeze | Stock-Grader Actions | NEW today; needs `SEC_CONTACT_EMAIL` repo secret, then dispatch once |

## Verification protocol for this handoff

```
cd Stock-Grader && pip install -e ".[dev]" && pytest -q          # expect ~570+, 0 failed
gh run list --repo TylerJForstrom/Stock-Data --limit 5           # snapshots + events green?
gh run list --repo TylerJForstrom/Stock-Vault --limit 5          # collectors green?
python -c "from stock_grader.data.vault import VaultDataSource;  \
  v=VaultDataSource('C:/Users/tforstrom/Desktop/Stock-Vault');   \
  print(len(v.market_eod_available_days()))"                     # expect 501+
```

Log progress in docs/REVIEW_FEEDBACK.md's Agent log, one line per completed
queue item, with commit hashes.
