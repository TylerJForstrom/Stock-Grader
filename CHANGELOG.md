# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
