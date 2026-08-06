# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **An ABSENT band verdict no longer passes the gate that was built to enforce it**
  (`stock_grader/signal_panel.py`, `stock_grader/cli.py`). The refusal below reads the verdict
  and short-circuits on its *presence* — `if adv_band is not None and not
  adv_band.get("reportable")` — so a banded panel whose sidecars carry no block at all was a
  silent pass. Every panel the small-cap programme was measured on is in exactly that state:
  they were built before `write_signal_panel` forwarded the verdict, so their `build.json` has
  no `adv_band` key, and the gate could not fire on the run whose write-up names the commit that
  added it. Two changes close it. `build_signal_panel` now REFUSES a banded namespace
  (`<signal>__adv<ID>`) whose observation manifest declares no `adv_band` — a panel that names
  itself a band and carries no verdict must not be manufactured. And `_load_adv_band` falls back
  to the producer's own observation manifest, recomposing the verdict through the same
  `band_verdict` the builder uses over the panel's own `counts.json`, labelling it
  `RECOMPOSED at evaluation time`; if even that is missing, a banded namespace raises rather
  than evaluating. An unbanded panel is untouched: no key in, no key out, no new behaviour.
  Re-checked against the eight measured panels — all eight now resolve a verdict from their
  observation manifests, all `reportable: true` at 47 evaluable periods against a floor of 30,
  so **no measured number moves**; the gate is now load-bearing on artifacts it was nominal on.

- **The licensing wall's gate could not see a docstring**
  (`tests/test_licensing_wall.py`, `tests/test_backtest.py`, `tests/test_band_reportable.py`).
  The gate globbed `docs/**/*.md` and nothing else, so a test docstring restating a band's
  measured capacity truncation from the private archive reached public main unflagged — the
  measurement belongs in Stock-Vault, and it is now stated there only. `src/` and `tests/` are
  scanned on the same terms, because a comment is published exactly as loudly as a document.
  The synthetic band fixture's dollar edges were also replaced with invented ones: the
  pre-registered bands were cut at the archive's measured quartiles, so a real edge is itself an
  archive measurement. Known limitation, recorded rather than papered over: a bare percentage
  matches no shape the gate looks for, and cannot be made to without firing on the specification
  (`1% of ADV20$`) far more often than on a leak. This gate is a floor, not a substitute for
  review.

- **The cross-repository cost pin asserted a byte-identity that was not true**
  (`stock_grader/config/cost_golden_vectors.json`, `tests/test_costs.py`). Both copies of the
  golden-vector file declared, in their own `note`, that "both repositories carry a
  byte-identical copy" — while the `bar_vectors` block added here landed on this side only, so
  the two files diverged, each suite asserted its own digest, and both stayed green. That is an
  attestation asserted rather than computed, which is the exact failure the pin was created to
  prevent. The note now describes the mechanism at the strength it holds: each side's literal
  catches an edit that does not update that side, and only Stock-Vault's CI — which can fetch
  this public file — catches a change landed in one repository and not the other; the reverse
  direction is not checkable and is no longer claimed to be. A new `estimator_contract` block
  declares the two things a porting implementation could not have guessed and which made a
  literal port impossible: `amihud_lambda_bps_per_musd` is bps of price per $1M traded (a raw
  |r|/$volume estimator must scale by 1e10), and `MIN_USABLE_PAIRS` is a property of the
  *composed* estimate, enforced inside the spread estimator here and one level up in the vault.
  Schema 1.2; the pinned digest moves and the same file lands in both repositories.

- **A band below the pre-registered evaluable-period floor is refused, not silently reported**
  (`stock_grader/signal_panel.py`, `stock_grader/backtest.py`, `stock_grader/cli.py`,
  `docs/REPORT-SCHEMA.md`). Stock-Vault computes a `reportable` verdict for every ADV band it
  exports — a band with fewer than the declared number of evaluable periods is written but marked
  NOT REPORTABLE — and stamps it on the observation dataset's manifest. Nothing consumed it. The
  return join copied the spec keys and dropped `adv_band`, so the evaluable panel the evaluator
  actually opens carried no band identity, no floor and no verdict; a band the programme refuses
  was indistinguishable from one it permits, and its numbers would have been quoted as a band
  result. `band_report` now composes the block from the vault's own verdict *and* this panel's
  measured `periods_in_panel`, and `write_signal_panel` publishes it in both `build.json` and
  `manifest.json`. The verdict is the AND of the two artifacts, so the thinner one binds; the
  floor is read from the producer rather than hardcoded here; a manifest that declares no floor
  yields `reportable: false` rather than a passing default. `stock-grader backtest` reads the
  hash-verified sidecar *before* it evaluates anything, exits 2 on a refused band, and burns no
  ledger trial doing so — a run that reports nothing must not charge the multiplicity budget.
  `--allow-unreportable-band` runs it anyway as an explicitly caveated exploratory look: the
  report leads with a `NOT REPORTABLE` limitation, the markdown heading says REFUSED, and the
  ledger line records it permanently. `BacktestReport` gains `adv_band`. A panel with no band
  metadata gains no key, no limitation and no new behaviour of any kind.

- **The participation cap is applied to the position, not just to the price of it**
  (`stock_grader/backtest.py`, `docs/COST-MODEL.md`). `estimate_cost` caps an order at 1% of
  `ADV20$` and prices the *capped slice*, but nothing downstream reduced the position: a
  truncated name stayed a full-weight member of its quantile bucket while being charged the cost
  of the fraction that fit. That converts a capacity constraint into a discount, and it does it
  hardest in the thinnest band — the band the small-cap programme exists to interrogate — where
  the cap binds on every row. Once the cap binds, participation is also pinned at the cap for
  every name, so the impact term stops varying with ADV at all. The evaluator now weights each
  row by `cost_notional_allowed_usd / cost_notional_target_usd`, so a name the cap could fill a
  fifth of holds a fifth of an equal-weight position, contributes a fifth of the exposure, and is
  charged its cost on those dollars; leg returns and leg costs are then both per *deployed*
  dollar and describe the same position. A leg the cap refuses entirely makes the period drop out
  counted rather than booking a fabricated 0%. `BacktestReport` gains `capacity_weighted` and
  `mean_deployable_fraction`, `PeriodResult` gains the per-leg deployable fractions and the
  truncated-name count, the markdown prints both, and every combination that does *not* apply the
  constraint — no capacity columns on the panel, or `capacity_weighted=False` — adds a limitation
  saying so in as many words. Stock-Vault's simulator has always reduced the filled quantity;
  until now the evaluator and the simulator meant different things by the same cap.

- **`rank_ic_net` subtracted cost with the wrong sign on the short leg**
  (`stock_grader/backtest.py`). The statistic subtracted a strictly positive round-trip cost from
  every name's forward return and ranked score against that — the net return of a long-only book.
  The bottom quantile is the short leg, where cost also destroys P&L, so the return to rank
  against there is `r + c`. Because expensive names cluster at low scores, the single-signed
  subtraction pushed the short leg's ranked returns further down and mechanically *raised* the
  correlation: on real banded panels the "cost-net" IC came out larger than the **gross** IC on
  runs whose net spread was negative, so the report table contained two cost-net numbers pointing
  opposite ways and the flattering one carried the more impressive name. It is now computed
  side-aware — `r - c` for the long half, `r + c` for the short half, `r` for the middle buckets
  nothing is held in — which can only attenuate. `PeriodResult.rank_ic_net` and
  `BacktestReport.mean_rank_ic_net` are renamed `rank_ic_net_side_aware` /
  `mean_rank_ic_net_side_aware` so a consumer of the old, differently-defined statistic fails
  loudly rather than silently reading a new one. `net_spread` was never affected. The previous
  regression test planted costs *uncorrelated* with score, where the bug is invisible; it is
  replaced by a correlated-cost case that reproduces the real configuration.

- **Corwin-Schultz omitted the overnight-gap adjustment its own docstring claimed**
  (`stock_grader/costs.py`, `config/cost_golden_vectors.json`, `docs/COST-MODEL.md`).
  `corwin_schultz_spread_bps` took highs and lows only, so the paper's adjustment could not be
  applied and was not, while the module docstring, `docs/COST-MODEL.md` and the split-exclusion
  rationale all described it — and the split exclusion justified itself by a mechanism the module
  did not have. The omission is one-sided: an overnight gap inflates `gamma` far more than
  `beta`, which shrinks `alpha` and shrinks the spread, so the public methodology of record
  reported a *narrower* spread than the model it documents, largest in the gappiest thin names.
  The estimator now requires `closes` and shifts the second session's range to touch the previous
  close (`gap_adjusted_range`), matching Stock-Vault's independent implementation. No shipped
  number moves: nothing in production calls the raw-bar estimators — production reads the
  liquidity inputs from the vault's observation part — but the public method now matches the
  published one.

  The golden vectors could not have caught it: every vector supplies `cs_spread_bps` as a scalar
  *input*, so the four raw-bar estimators were not pinned at all. `bar_vectors` is added to
  `config/cost_golden_vectors.json` (schema 1.1) — raw bars in, spread/sigma/lambda out, plus
  what the same tape produces *without* the adjustment so an implementation that drops it fails
  rather than agreeing on a narrower number. The three tapes were cross-checked against
  Stock-Vault's implementation in one process and agree to the last bit; on the gapped tape the
  adjusted estimate is 128.9 bps against 0.0 unadjusted. The golden sha256 moves from
  `58d7105d…` to `53684bf6…`; Stock-Vault must take the same file.

### Added

- **Per-row trading costs** (`stock_grader/costs.py`, `docs/COST-MODEL.md`). The evaluator
  charged one flat rate in basis points to every name in every period. That rate undercharges
  thinly traded names and overcharges liquid ones, so any comparison of a signal across
  liquidity tiers had a thumb on the scale in favour of the thin tier — the exact comparison a
  "small caps are less efficient" claim rests on. The replacement prices each name-date from its
  own liquidity state: a Corwin & Schultz (2012) high-low spread over a 21-session window
  (negative two-day estimates floored ONCE on the window mean, never per pair, and floored again
  at the one-cent tick), plus Almgren, Thum, Hauptmann & Li (2005) impact at their published
  calibration for a full-session order, floored by the archive's own Amihud (2002) coefficient —
  the larger of two documented estimators, a conservatism rule declared in advance rather than a
  fitted blend. No simulated order exceeds 1% of 20-session dollar ADV, and truncation is counted
  and reported rather than applied silently.

  The old flat charge is recovered exactly as a degenerate case of the new one (`CS = 0`, tick
  floor 10 bps, zero volatility, zero lambda, no cap gives 5 bps one way and a 10 bps round
  trip), and that is `GOLDEN_VECTORS[0]` in `config/cost_golden_vectors.json` rather than a
  remark. Stock-Vault implements the same model independently — the two repositories may not
  import each other — so that file is the pin: both carry byte-identical copies and both assert
  the sha256 of its canonical JSON content. Canonical rather than raw bytes because git rewrites
  line endings on checkout, and a cross-repository agreement that fails on a Windows runner's
  `core.autocrlf` is not an agreement; `tests/test_costs.py` pins that with a CRLF rewrite.

  The v6 return join writes `round_trip_cost_bps` and its components onto the evaluable panel,
  priced at the entry close and at a declared per-position notional, and carries the raw inputs
  alongside so a capacity ladder is a recomputation over the existing panel rather than a rebuild
  of it. A row whose window is too short to measure, or whose observations lack any of the five
  liquidity inputs, gets no cost at all — counted in `no_cost_estimate_rows`, never back-filled
  from a tier average. `build.json` records the model id, the notional, the coverage, the
  truncation counts and the golden-vector hash; a net number whose cost model and position size
  are not on the artifact is not reproducible.

  `evaluate_walk_forward` charges each quantile leg the equal-weight mean cost of the names
  actually in that leg, applied to that leg's turnover — the same shape the flat charge always
  had, with a per-leg rate instead of a global constant. **A panel that carries no cost column
  evaluates bit-for-bit as before**: the original single-constant expression is kept deliberately
  (`rate * (a + b)` differs from `rate * a + rate * b` in the last bit of a float, and a
  published number that moves in its last bit is a number that has moved), and
  `tests/test_backtest.py` pins the whole flat report against literals transcribed from the
  pre-change implementation.

### Fixed

- **2026-08-04 audit**: nineteen confirmed defects, all of the same shape — something
  optimistic happening silently where the ecosystem's rules require a computed number or an
  honest refusal. Every fix carries a regression test in `tests/test_audit_regressions.py`
  that fails at `f90986b`.

  *Evaluable signal panel.* `panel.parquet` was built from a filesystem glob while every
  whole-panel number and all three attestations were computed from `counts.json`, with nothing
  reconciling the two: the writer now refuses a part the accounting does not cover, refuses a
  rollup whose per-date row counts disagree with `counts.json`, refuses a `--rebuild` that
  re-prices a closed date to zero rows (rather than deleting the part or silently keeping both),
  and writes `counts.json` atomically *before* the parquet parts so a crash can only leave
  accounting ahead of parts — the direction the next run heals. A signal date whose entry day is
  absent from the vault clone is refused instead of losing 100% of its observations and vanishing
  from the panel, and `survival_rate` / `panel_observations` / `no_start_price_rows` /
  `periods_in_panel` are now surfaced. `unresolved_tickers` is persisted per signal date and
  aggregated from `counts.json`, so the field in build.json's whole-panel block is actually
  whole-panel (dates closed before the field existed are named in
  `unresolved_tickers_incomplete_dates`, never papered over with an empty union). A
  caller-supplied `license_note` can no longer discard the Massive / dividend-archive /
  stockanalysis.com provenance clause through `+`-before-`or` precedence.

- **Split guard: the foundry table is now a DETECTOR, not a confirmer.** `_foundry_split_lookup`
  was reachable only after `detect_split` had already matched a price signature, and
  `PLAUSIBLE_SPLIT_RATIOS` floors at 1.5 by design — so every 5:4, 6:5 or 1.2:1 split recorded in
  the authoritative table was invisible in both directions, kept with `split_factor=1.0` and a
  forward return fabricated by the whole size of the split. `split_factor` now applies every
  foundry split effective in `(entry, exit]` at any ratio, attributed to the bar pair whose
  interval contains its effective date and arbitrated against the observed move; a ratio the
  price contradicts, one outside `FOUNDRY_SPLIT_RATIO_BAND`, or one no bar pair can place is
  UNRESOLVED — dropped and counted, never a silent 1.0. The dividend leg now keys on "a split
  event occurred in the window" rather than `factor != 1.0`, which had been declaring the
  per-ex-date share basis safe for exactly the splits the price signature could not see.

- **Research ledger.** `append_record` returns the CHAINED record; `promotion-declare` (both
  modes) and `decay.record_sweep_trials` report/store that hash instead of the pre-append
  object's, which was never the hash on disk and produced evidence pointers that resolve to
  nothing. `cmd_backtest` and `record_sweep_trials` now refuse a ledger whose chain does not
  verify, as every appending verb already did, and `trial_sharpes_by_experiment` skips a line
  that does not hash to its own claim so a forged `per_period_sharpe` cannot set the deflation
  dispersion. `ledger-retract` refuses a `ledger:promotion` record: retraction is a
  trial-accounting act with no effect on a trials=0 record, and its only real consequence was
  rewinding `promotion_stage` — un-retiring a terminal subject or undoing a demotion without the
  reason and evidence a downward transition requires. `promotion-declare` now refuses a policy
  version the document does not name and refuses binding a NEW version to a document already
  declared under another, closing the reproduced path where `--live-money-reachable` opened the
  money rung against bytes that say the rung is closed. Evidence pointers must be 64-character
  lowercase hex, so `[""]` no longer satisfies "name the records it rests on", and
  `promotion_stage` seeds from the DECLARED ladder rather than the module constant.

- **Frozen-panel catalog.** `refresh_frozen_manifest` refused nothing: bytes that changed under a
  cataloged name were re-hashed and re-blessed as `hashed_at: "backfill"`, so the next monthly
  freeze laundered exactly what `verify_sibling_manifest` refuses, and an unreadable or
  future-schema manifest was destroyed and rebuilt over whatever was on disk. Both are now
  refusals on the existing red-freeze path. `verify_sibling_manifest` grows `strict=`, used by
  `backtest`/`ledger-declare` (opt out with `--allow-unmanifested-panel`, which records the input
  as UNATTESTED in the ledger line), and `build_panel` records `frozen_inputs_attested` and gates
  `ready_for_backtest` on it — a panel nothing vouches for can no longer become forward evidence
  indistinguishably from an attested one.

- **Cadence + monthly workflow.** The freeze clock watched only `frozen_scores`, so
  `frozen_scores_wide` — half the committed forward evidence, written and committed by the same
  monthly cron — could stall indefinitely while `check-cadence` reported PASS (it is stale for
  2026-08 right now, and the fixed clock says so). `check_cadence` now evaluates
  `FROZEN_ROOTS` and fails if ANY root is stale; `--frozen-root` is repeatable. The
  pre-registered look schedule names `workflow_dispatch`, which runs the identical
  ledger-appending path and which `ledger-declare`'s idempotence would have frozen out of the
  declaration forever. Dated forward reports are placed through a temp file with a
  refuse-on-different-content guard instead of a bare `>` redirect that truncated the destination
  *before* the command ran — a failing run could destroy a previous run's report and commit a
  0-byte evidence artifact.

- **Licensing wall.** Removed vault-derived measured values from this PUBLIC repo's design docs
  (`docs/majors/M1`–`M5`, `docs/MAJOR_IMPROVEMENTS.md`): per-ticker IB borrow fees and their
  distribution, FINRA file composition, Finnhub coverage and analyst totals, an SSGA CUSIP, a
  named ticker's fractional Massive EOD volume and per-ticker closes, dollar-volume figures by
  screen rank, and two complete measured panel/join results. Schema, field names and the
  algorithmic spec are unchanged — only the measurements are gone, and they belong in
  Stock-Vault. `tests/test_licensing_wall.py` is the standing gate.


### Added

- `stock_grader.signal_panel` and `stock-grader build-signal-panel`: the return join for
  Stock-Vault's raw signal observations, making this repository the **single owner of
  forward-return semantics** for both panel families. The computation previously existed twice —
  `stock_vault/prices.py`'s `resolve_forward_return` was a documented port of
  `panel._resolve_exit_price` — and the copies had diverged: this repository's
  `PLAUSIBLE_SPLIT_RATIOS` carries 1.5 and 2.5, the port's did not, so a 3:2 forward split
  matched no ratio there, tripped no guard, and survived as a fabricated ~-33% forward return,
  while this side had already moved to dividend-inclusive total return. ECOSYSTEM rule 1 forbids
  the cross-repo import that would deduplicate it, so the *computation* moved: the vault now
  exports observations (signal, pre-registered score, entry-side cross-section, the outcome
  window) and this builder joins returns through the same `split_factor` /`resolve_exit_price` /
  per-ex-date dividend chain `build-panel` uses. `tests/test_signal_panel.py` plants that exact
  3:2 split and proves the fabricated return is dead.

  Attestations are computed, never declared: `universe_is_pit` requires full point-in-time
  membership *and* zero outcome-dependent drops (partial coverage reports
  `pit_membership_coverage` instead of rounding up), `return_is_total` requires measured dividend
  coverage at `TOTAL_RETURN_COVERAGE_BAR`, and a zero-length return window (`return_end <=
  return_start`) is refused rather than priced as a table of zeros. Per-date parts are immutable
  and whole-panel accounting is restored from the persisted `counts.json`, so an incremental
  monthly run and a full rebuild report identical attestations. The joined rows derive from
  Massive free-tier closes on top of restricted signal sources, so the builder writes only into a
  private Stock-Vault clone and raises if the output path escapes it; nothing it produces is
  committed here.

  Supporting changes: `VaultDataSource.signal_panel_signals` / `signal_panel_manifest` /
  `signal_panel_observations` (manifest-listed, sha256-verified reads of the observation parts);
  `panel._resolve_exit_price`, `_window_months` and `_vault_manifest` are now the public
  `resolve_exit_price`, `window_months` and `write_vault_manifest` (the last gains an additive
  `extra` block, mirroring `stock_vault.manifest.write_manifest`).

- `stock_grader.cadence` and `stock-grader check-cadence` / `stock-grader forward-accounting`:
  expectation clocks for the monthly evidence loop (docs/CADENCE.md). Every
  monthly-forward-backtest run now writes `docs/forward/<YYYY-MM>/accounting.json`
  unconditionally — each profile's evaluated / not-matured / refused state from a closed
  vocabulary — so "the loop ran and nothing matured" is a recorded fact rather than silence.
  `check-cadence` verifies, by jitter-tolerant days of the month, that the current month's
  accounting artifact exists and that `frozen_scores`' newest freeze is current (closing
  monthly-freeze's documented lack of a staleness gate); it runs as a bootstrap-guarded
  self-gate in monthly-forward-backtest and as a weekday cross-coverage check from
  Stock-Vault's shadow-arms workflow, which executes `cadence.py` as a bare stdlib-only
  script from its existing Stock-Grader checkout.

- `stock_grader.frozen_manifest`: sibling `manifest.json` catalogs for score panels — the same
  manifest convention the foundry and vault datasets carry (schema_version 1.0, per-file
  sha256/bytes), extended with per-file `rows`/`columns`, honest `hashed_at` provenance
  (`freeze`/`build`/`backfill`), and a dataset-content version (`content_schema_versions`, the
  panels' row `schema_version`) kept deliberately distinct from the manifest-format version.
  `freeze` writes and additively refreshes the catalog for every `frozen_scores*/<profile>/`
  directory; `build-panel` catalogs its output directory; existing frozen directories were
  backfilled in place (parts untouched, entries marked `backfill`). Every panel-consuming
  loader (`backtest`, `ledger-declare`, `build-panel`, `decay`) now verifies a panel's sha256
  against the sibling manifest when one exists, refusing on mismatch or on a part the catalog
  does not know; a directory without a manifest predates the convention and loads with an
  `UnmanifestedPanelWarning`. `.gitattributes` marks `*.parquet -text` so no platform can
  line-ending-translate a hash-pinned part.

- `stock_grader.peers`: deterministic comparable-company selection with same-business-model,
  SIC, reporting-currency, and market-cap rules plus a fingerprinted selection manifest.
- `stock_grader.research` and `stock-grader research`: a versioned analyst evidence bundle
  containing the grade, explicit or automatically selected peers, raw and normalized metric
  evidence, peer distributions, fiscal trends, provenance, valuation scenarios, and warnings.
- `stock_grader.valuation`: transparent bear/base/bull and reverse-DCF APIs based on an explicitly
  labelled levered cash-flow proxy. The API refuses business models for which that proxy is
  inappropriate.
- `stock_grader.backtest` and `stock-grader backtest`: validation of point-in-time panel
  contracts, cross-sectional rank IC, equal-weight quantile spreads, turnover and fixed transaction
  costs, moving-block intervals, drawdown, and purged chronological split helpers. The CLI
  requires filing-cutoff, PIT-universe, total-return, delisting, and permanent-identifier evidence
  unless an exploratory override is explicit.
- Explicit `--price-provider auto|csv|tiingo|stockanalysis|yahoo|sec|none` selection. The existing
  Tiingo provider is now reachable from the CLI when `TIINGO_API_KEY` is configured.
- JSON and Markdown support for rankings and profile consensus, with `--top` applied consistently
  across ranking formats.
- Machine-readable `sensitivity_interval`, letter scenario frequencies, effective pillar
  weights, lost weight, gates, and consensus inclusion details.
- Price-frame validation and diagnostics for adjusted-price status, invalid observations,
  duplicate dates, stale or sparse histories, and OHLC consistency.
- Bank-oriented metrics, REIT FFO reconstruction, SEC-derived scalar price support, FRED
  benchmark/risk-free inputs, CIK-based historical fetching, and point-in-time filing selection.

### Changed

- Named profiles default to a peer-relative cross-sectional curve.
- Reports call the 5th–95th model-perturbation range a **model-sensitivity interval**, not a
  statistical confidence interval. The compatibility field `ci` remains in serialized reports.
- Documentation now treats every peer-normalized composite—including both components of the
  optional hybrid curve—as universe-dependent.
- Supervised weighting is rejected in the same cross-section by default; historical callers must
  fit only on information available before the test period.
- Consensus excludes `N/A` profile reports and preserves the grading curve used by its included
  reports.
- Metric contribution reporting uses effective rather than merely nominal weights when data gaps
  remove pillars.

### Fixed

- Historical `--asof` requests can no longer silently use later restatements under the default
  latest-vintage mode.
- Duration-fact normalization distinguishes discrete quarters, year-to-date facts, and fiscal
  years; averaged share counts are not treated as additive flows.
- Foreign-currency facts are not silently interpreted as USD.
- Abandoned tags, split-scaled share counts, mixed restatement vintages, invalid valuation
  denominators, and adjusted-vs-traded price use now have explicit guards.
- Missing benchmark or risk-free data no longer silently becomes a zero-rate assumption.
- Metrics that are undefined for a business model are distinguished from genuinely missing data.

### Validation status

- `scripts/calibrate_intervals.py` is an internal missing-data/self-consistency stress test. Its
  output does not establish statistical coverage for an unknown true grade or for future returns.
- `scripts/validate_distress.py` measures contemporaneous separation between an EDGAR text-labelled
  sample and a selected control group. Its AUC is not evidence that grades predict distress or
  stock returns out of sample.
- No bundled survivorship-free historical panel or audited out-of-sample return result is claimed.
  The new backtest module supplies an evaluation contract; the caller must supply lawful,
  point-in-time universe membership and total-return data including distributions and delistings.

## [0.1.0]

Initial implementation of the grading pipeline, profiles, registries, SEC EDGAR XBRL data layer,
terminal reports, and core fundamental/statistical metrics.
