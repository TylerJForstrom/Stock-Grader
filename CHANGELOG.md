# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
  its sha256.

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
