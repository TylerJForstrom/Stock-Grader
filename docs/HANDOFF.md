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
DONE (2026-08-04). Canonical = SEC dash form, spec written into Stock-Data's
ECOSYSTEM.md (rule 7). `ticker_variants()` now carries the IB space form and
`canonical_ticker()` is the shared dash-form canonicalizer; the two ad-hoc
space-form expansions in VaultDataSource are gone, `FoundryDataSource.trailing_dps`
matches all spellings instead of exact-equality, and the SEC insider table
canonicalizes tickers on read (cache schema v3 — dot/space/dash spellings of one
issuer share one median row). The FINRA no-separator form (`BRKB`) is deliberately
NOT a variant: a squashed class share can spell another issuer's real ticker, so
no-separator joins go through Stock-Vault's ambiguity-guarded `build_squash_index`.
Stock-Data got its own copy (`src/stock_data/tickers.py`, applied in
`resolve_ciks`); Stock-Vault already had `tickers.py` covering dash/dot/space/squash
with the same canonical form, so no vault change was needed.

### 5. events.jsonl into the manifest contract + universe(asof)  (~half day)
DONE end-to-end (2026-08-04). The events dataset has its own manifested dir
(`data/symbols/events/`), `FoundryDataSource.universe(asof=...)` replays
added/removed/changed events backward from current, and — the final leg —
`build_panel` now resolves its missing-CIK fallback through
`universe(asof=signal_date)` PER signal date, falling back to the current
snapshot only when the archive cannot reach the date (recorded per date in
the sidecar's `cik_map_modes`, never attested). Stock-Vault's signal-panel
builder gained the same per-date replay in its `v5` layout
(`stock_vault/panels.py _CikMaps`, docs/SIGNAL-PANELS.md "Panel v5").

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
- ~~Supervised guard~~ DONE (2026-08-03): split into two refusals — the
  leakage block (returns present without opt-in) and a distinct
  "no forward returns supplied" input error replacing the silent
  equal-weights fallback. The opt-in flag unlocks only the former; the
  no-returns case now refuses instead of degrading, which is stricter than
  the pass-through this bullet originally sketched.
- ~~`winsorized_z`~~ DONE (2026-08-03): clamps at median±3·MAD
  (σ-consistent) when n<100; MAD==0 keeps the quantile clamp so a real
  difference is not flattened onto the median; n≥100 behavior unchanged.

### 9. Consumption layer  (~2 days)
- ~~Run journal + `diff --since-last`~~ DONE (2026-08-03): `journal.py`
  appends every `grade` run under `~/.stock-grader/runs/` (immutable records
  keyed by asof+fingerprints; `--journal-dir` overrides, `--no-journal`
  disables); `stock-grader diff --since-last TICKER` refuses config/peer-set
  regime breaks (`--allow-fingerprint-drift` accepts one, visibly) and
  reports letter/score/pillar deltas plus the metric contributions that
  moved them. `grade --hysteresis` seeds `apply_hysteresis` with the newest
  comparable run's letters — opt-in per SPEC design decision D9 (prior state
  is an explicit input). Because the canonical universe fingerprint hashes
  each member's asof (data vintage), run-over-run comparability uses a
  vintage-free membership fingerprint over (ticker, cik, sector); `rank`
  runs are not journaled yet.
- `ecosystem-status` command in Stock-Data: one table — every clock, last
  tick UTC, staleness vs cadence, via raw GitHub URLs + local vault clone.
- ~~Dossier "What drove this grade"~~ DONE (2026-08-03): `research_to_markdown`
  renders top-5 strengths/concerns in words from the exact explain-payload
  contributions plus an attribution-identity line; zero-contribution OK rows
  collapse to a named count. The §4 dossier additions (per-peer comp table,
  SBC, insider activity, red-flag display) remain deferred in item 10.
- Provenance header (asof + fingerprint prefixes) in the two markdown
  renderers.

### 10. Deferred (needs data volume or a decision)
- ~~§6 panel builder~~ DONE (M1, 2026-08-02): `stock-grader build-panel` +
  `monthly-forward-backtest.yml`. ~~Backtest calibration harness (planted-IC
  power table)~~ DONE (2026-08-03): `scripts/power_table.py` +
  `docs/calibration/power_table_2026-08-03.{md,json}` — read it before the
  first real forward verdict.
- ~~Second return chain in Stock-Vault~~ DONE (2026-08-04, panels v6):
  `stock-grader build-signal-panel` (`stock_grader.signal_panel`) is now the
  single owner of forward-return semantics for BOTH panel families. The vault
  exports raw observations and its ported `resolve_forward_return` is deleted;
  the vault's `signal-panels.yml` checks this repo out and runs the join in the
  same job. Remaining known duplication: the vault's shadow execution simulator
  keeps its own split handling — it simulates positions, not panel returns.
- Li-Mohanram mechanical forecasts (Stock-Data derived artifact; spec in
  docs/DATA-FOUNDRY.md).
- ~~Paper-trade bridge~~ DONE (M3, 2026-08-03): shadow arms in Stock-Vault
  replay `paper.target_portfolio` over every profile's frozen panels against
  the EOD archive; one journal per arm, byte-deterministic, calibrated against
  the real account's fills.
- §4 dossier additions (per-peer comp table, SBC, insider activity, red-flag
  display from the events dataset).
- ~~Pre-registration record kind~~ DONE (2026-08-04): `ledger:preregistration`
  records in research_manifest (spec JSON + SHA-256 + declared schedule),
  `stock-grader ledger-declare` (idempotent, declare-if-absent), and
  spec-hash matching in `cmd_backtest` — a matched run is a PRIMARY
  re-evaluation recorded under the declaration's spec-bound experiment name,
  REPLACING its entry in the shared denominator (trial count stays flat);
  unmatched runs keep the old per-filename behavior. The monthly workflow
  declares before evaluating. Peeking is disclosed via the declared schedule,
  not corrected — see docs/VALIDATION.md "Pre-registration".

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
