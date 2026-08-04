# M1: Forward panel joiner (`stock-grader build-panel`) + monthly scheduled forward backtest

> Part of the major-improvements handoff. Read [`../MAJOR_IMPROVEMENTS.md`](../MAJOR_IMPROVEMENTS.md)
> first — it carries the orientation, the working rules, and the milestone ordering.

> ## Status update — landed since these specs were researched
>
> These specs were written earlier on 2026-07-30. The work they describe as "in flight", "in flux", "being
> edited right now", or "must land first" has since **landed, been adversarially reviewed, and been pushed**.
> Wherever a line below tells you to wait for it, coordinate with another agent, or treat a file as a moving
> target, that instruction is **superseded**. All three repos are clean and green:
>
> - **Stock-Grader `5290a2f`** (582 tests) — `freeze --all-profiles` writes
>   `frozen_scores/<profile>/YYYY-MM-DD.parquet` across 11 registered profiles. Nine panels exist for
>   2026-07-30; `momentum` and `low_volatility` refused because they cannot grade without a dense price
>   series. `monthly-freeze.yml` runs `--all-profiles` and is verified green in the cloud. A refusal only
>   fails the run when the traded profile refuses, a profile that froze before refuses now, or nothing was
>   written — a structural refusal keeps the run green on purpose.
> - **Stock-Vault `b407524`** (73 tests) — `load_frozen_panel(source, profile="all_weather")` reads the
>   per-profile layout, with the flat layout kept as a documented legacy fallback, so the real trader is NOT
>   broken; it has in fact traded (10 orders on 2026-07-30). The journal now emits `kind: "benchmark"` and
>   `kind: "fill"`. **The fill record's drift field is `drift_bps`, with `reference_close` and
>   `reference_date`** — deliberately not named slippage, because the free EOD archive lags two sessions.
>   `staleness.check_paper` now reads broker-sourced `snapshot` records rather than the journal manifest.
> - **Stock-Data `15b0ad2`** (38 tests).
>
> Still outstanding and still yours to handle where your milestone needs them: the `VAULT_REPO_TOKEN` PAT,
> and the **unwired vault price provider** described in [`../MAJOR_IMPROVEMENTS.md`](../MAJOR_IMPROVEMENTS.md)
> — `momentum` and `low_volatility` produce no evidence at all until that lands.
>
> **Line numbers in these specs predate the landed work. Re-read every file before you edit it.**

**Effort:** large

**Why it matters:** Close the evidence loop: frozen point-in-time score panels are joined to realized forward returns and evaluated on a schedule, so every month of freezing turns into a recorded, multiple-testing-corrected trial instead of an un-evaluated file. Without this, the forward record accrues but never answers whether any profile ranks stocks better than chance. It also fixes the two ledger defects that would make the first real verdict uninterpretable.

## Prerequisites

- ~~The in-flight multi-profile freeze must land first~~ **DONE (5290a2f)**: `cmd_freeze` writes `frozen_scores/<profile>/YYYY-MM-DD.parquet` and nine panels exist for 2026-07-30. `discover_frozen_panels` targets that layout only.
- A new Stock-Grader repository secret `VAULT_REPO_TOKEN`: a fine-grained PAT with Contents read+write on TylerJForstrom/Stock-Vault. Without it the workflow cannot read the private EOD archive or archive the built panel.
- `data/symbols/events/manifest.json` in Stock-Data (HANDOFF item 5) — VERIFIED already present on disk, so `FoundryDataSource.universe(asof=)` works today. No action needed.
- OPTIONAL, improves but does not block: HANDOFF item 6 (writing `cik` into `delisted_prices/*/cohort_index.json`) would upgrade delisting resolution from symbol matching to CIK matching.
- OPTIONAL, unblocks the total-return attestation: a per-ex-date cash-dividend dataset (ex_date, cash_amount, unadjusted basis) covering the panel universe. Until it exists `return_is_total` stays False by design.

## Verified ground truth

Every line below was confirmed by reading the cited code or data file. Re-verify anything that looks
stale before relying on it — and if the code contradicts this document, the code wins: say so in your
report rather than bending the code to match the doc.

- `_validate_panel` requires exactly these six columns; anything else raises `ValueError('backtest panel is missing columns: ...')`: `signal_date`, `return_start`, `return_end`, `ticker`, `score`, `forward_return`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:116-126`)*
- If `filed_through` is present it is parsed as a date, must have no NaT, and `frame['filed_through'] > frame['signal_date']` on any row raises 'N observations use filings after their signal_date'.  
  *(`src/stock_grader/backtest.py:132-140`)*
- `return_start` must be STRICTLY after `signal_date` and `return_end` STRICTLY after `return_start`; equal dates raise.  
  *(`src/stock_grader/backtest.py:141-144`)*
- Duplicates are checked on BOTH `(signal_date, ticker)` and `(signal_date, _security_key)` where `_security_key` is the permanent id column; either raises 'duplicate signal_date/security observations are not allowed'.  
  *(`src/stock_grader/backtest.py:151-157`)*
- Rows whose `score` or `forward_return` is non-finite are SILENTLY dropped (`finite` mask), and `forward_return < -1.0` raises 'forward_return cannot be below -100%'. Exactly -1.0 (total loss) is legal.  
  *(`src/stock_grader/backtest.py:158-163`)*
- `_attested(panel, column)` returns True only when the column exists, has NO NaN, and EVERY value stringifies (lowercased, stripped) into {'1','true','yes','y'}. A python bool True passes; False/0 fails. There is no per-row partial attestation.  
  *(`src/stock_grader/backtest.py:167-176`)*
- `_permanent_id_column` scans `('cik','security_id','permanent_id')` and accepts the first whose values have NO NaN and are all non-blank after `.astype(str).str.strip()`. One null CIK anywhere fails the whole contract item.  
  *(`src/stock_grader/backtest.py:179-185`)*
- The five contract keys are exactly: `filing_cutoff_provided`, `point_in_time_universe_attested` (column `universe_is_pit`), `total_returns_attested` (column `return_is_total`), `delistings_included_attested` (column `delisting_return_included`), `permanent_identifier_present`.  
  *(`src/stock_grader/backtest.py:188-196`)*
- A signal_date group is rejected (counted in `rejected_periods`) when `len(group) < config.min_cross_section` (default 20) or `group['score'].nunique() < 2`; and a signal_date whose rows carry more than one distinct `return_start`/`return_end` raises 'signal_date ... mixes return windows'.  
  *(`src/stock_grader/backtest.py:277-287`)*
- If no period survives, `evaluate_walk_forward` raises ValueError('no period met the minimum cross-section and score-dispersion requirements').  
  *(`src/stock_grader/backtest.py:326-329`)*
- First-period turnover is 1.0 by design (`_turnover` treats an absent prior portfolio as cash), so a 1-period backtest is charged 2x the cost rate on its net spread.  
  *(`src/stock_grader/backtest.py:199-207`)*
- `cmd_backtest` raises unless EVERY contract item passes, unless `--allow-unverified-panel` is set: 'panel fails the strict input contract (...)'.  
  *(`src/stock_grader/cli.py:766-775`)*
- `cmd_backtest` builds `trial_sharpes` as EVERY prior ledger record's `metrics['per_period_sharpe']` that is finite, appends this run's, and calls `assess_edge(net_spreads, trial_sharpes, periods_per_year=args.periods_per_year, bootstrap_seed=args.seed)` only when `len(net_spreads) >= 2`.  
  *(`src/stock_grader/cli.py:789-814`)*
- Each run appends `ResearchRecord(experiment=f'backtest:{path.name}', ..., trials=len(trial_sharpes), metrics={'per_period_sharpe','mean_net_spread','mean_rank_ic','deflated_sharpe'}, leakage_controls='panel attestation contract: PASS|FAILED <names>', gate_passed=..., verdict=..., data_span='<first>..<last>', code_commit=current_commit())`. The experiment name is derived from the panel FILENAME.  
  *(`src/stock_grader/cli.py:815-853`)*
- `cmd_freeze` now writes `out_dir/<profile>/<YYYY-MM-DD>.parquet` (multi-profile), never overwrites an existing date, and refuses a profile whose graded count is below `config.min_letter_peers`.  
  *(`src/stock_grader/cli.py:902-987`)*
- Frozen panel row schema (exact keys written): signal_date, ticker, cik, score, letter, percentile, coverage, graded, profile, config_fingerprint, universe_fingerprint, code_commit, schema_version. There is NO `filed_through` column.  
  *(`src/stock_grader/cli.py:953-970`)*
- Verified on disk: the only frozen panel today is `frozen_scores/all_weather/2026-07-30.parquet`, 82 rows, 78 graded, ungraded = BRK-B, UNH, V, XOM. `cik` is a zero-padded 10-char string ('0000320193'), zero nulls, zero blanks, 82 distinct CIKs (no dual-class collision today).  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/frozen_scores/all_weather/2026-07-30.parquet`)*
- `assess_edge` sets `significant = dsr >= (1-alpha) and ci_lo > 0.0`; `block_bootstrap_sharpe_ci` returns `(0.0, 0.0)` when `len(returns) < block + 1` (block default 10), so fewer than 11 periods can NEVER be significant; and the verdict is 'INSUFFICIENT SAMPLE' when n < 30.  
  *(`src/stock_grader/significance.py:196-198, 323-326`)*
- `deflated_sharpe_ratio` benchmarks against `expected_max_sharpe(stdev(usable), n_trials)` — the DISPERSION of prior trial Sharpes drives the deflation, so one absurd trial Sharpe poisons every later verdict.  
  *(`src/stock_grader/significance.py:170-183`)*
- `append_record` overwrites `prev_sha256` with the previous line's `integrity_sha256` at write time; `verify_chain` detects deletion/reordering/rewriting inside the chained suffix. Records are never mutated — corrections must be APPENDED.  
  *(`src/stock_grader/research_manifest.py:126-200`)*
- `ResearchRecord` fields available for a correction record: experiment, market, symbols (Sequence[str]), targets, horizons, trials, metrics (Mapping[str, float|None]), costs, benchmark, leakage_controls, gate_passed, verdict, data_span, code_commit, created_utc, prev_sha256.  
  *(`src/stock_grader/research_manifest.py:63-87`)*
- The live ledger `research_ledger.jsonl` contains 12 records, ALL with `experiment == 'backtest:panel.csv'` and `metrics.per_period_sharpe == 2.8284271247461894` — synthetic CLI-test panels, not market hypotheses. `summarize_manifest` reports `all_integrity_ok: True, chain_ok: True`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/research_ledger.jsonl`)*
- `VaultDataSource.market_eod_day(day)` reads+sha256-verifies `data/market_eod/<YYYY-MM>/<YYYY-MM-DD>.jsonl.gz` and returns a DataFrame; `market_eod_available_days()` lists every archived date from the filenames.  
  *(`src/stock_grader/data/vault.py:85-104`)*
- `VaultDataSource.market_eod_series(ticker)` loops over EVERY available day and calls `market_eod_day` per day — 501 gz reads + 501 sha256 verifications PER TICKER. It must not be used inside the panel builder.  
  *(`src/stock_grader/data/vault.py:106-131`)*
- `VaultDataSource.delisted_history(symbol)` searches `data/delisted_prices/<year>/` dirs in REVERSE year order, returns the first match as a DataFrame indexed by date. Verified: for AATC the index is DESCENDING (2026-07-24, 2026-07-23, ...) with columns a, c, ch, h, l, o, t, v.  
  *(`src/stock_grader/data/vault.py:161-193`)*
- market_eod is collected with `params={'adjusted':'false','include_otc':'false'}` — RAW unadjusted closes, listed venues only. A name that moves to OTC vanishes from the archive.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/market_eod.py:38-41`)*
- market_eod row keys are exactly: symbol, open, high, low, close, volume, vwap, transactions. There is NO date field in the row — the date is only in the filename.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/market_eod.py:47-59 and data/market_eod/2026-07/2026-07-28.jsonl.gz`)*
- Verified on disk: roughly two years of archived market days, one whole-market file per session. A spot-checked day file had no null or zero closes and no duplicate symbols; a handful of rows carry a null `transactions`, which is why volume corroboration returns False without it. (Row counts and date spans are vault-derived and stay in Stock-Vault.)  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/`)*
- Verified split hazard is REAL in the raw archive: on 2026-07-28 there are 18 one-day moves beyond +/-45%, including AMKL at exactly -50.0% (11.75 -> 5.88) and AMKX at -47.8% — a textbook forward-split signature.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/2026-07/2026-07-27.jsonl.gz vs 2026-07-28.jsonl.gz`)*
- Verified spelling mismatch: the frozen panel's `BRK-B` (SEC dash form) is ABSENT from market_eod; `BRK.B` (Polygon dot form) is present. `ticker_variants(t)` returns (as-given, dot->dash, dash->dot).  
  *(`src/stock_grader/data/symbols.py:13-23`)*
- Verified universe churn: 386 symbols present on 2026-06-30 are absent on 2026-07-28, and 394 are new — an inner join on symbol silently loses names.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/`)*
- `data/corporate_actions/dividends.parquet` has 448 rows covering only 3 tickers (AAPL, JNJ, O). Its manifest states `"granularity": "fiscal_period (no ex-dates in XBRL)"` and `"basis": "current_fully_split_adjusted"`. It CANNOT support total returns against raw unadjusted prices.  
  *(`C:/Users/tforstrom/Desktop/Stock-Data/data/corporate_actions/manifest.json and dividends.parquet`)*
- `data/corporate_actions/splits.jsonl` contains exactly 2 rows, both AAPL (2014-06-06 ratio 7.0, 2020-08-28 ratio 4.0). Fields: cik, confidence, effective_date, filed, flags, ratio, ticker. Coverage is far too thin to be the sole split defense.  
  *(`C:/Users/tforstrom/Desktop/Stock-Data/data/corporate_actions/splits.jsonl`)*
- `FoundryDataSource` already exposes `splits()` (parses effective_date to Timestamp), `dividends()`, `trailing_dps(ticker, asof=)`, and `universe(listed_only=, asof=)` which replays `data/symbols/events/events.jsonl` backward and RAISES for asof before the archive boundary.  
  *(`src/stock_grader/data/foundry.py:113-226`)*
- `data/symbols/events/` exists with its own manifest.json (628 rows, schema_version 1.0) — HANDOFF queue item 5 is already done. Event rows are {date, event: added|removed|changed, record: {cik, ticker, title}, source}.  
  *(`C:/Users/tforstrom/Desktop/Stock-Data/data/symbols/events/`)*
- Delisted cohort files: `data/delisted_prices/<year>/<SYMBOL>.json.gz` with payload `{"data": [{a,c,ch,h,l,o,t,v}], "status": ...}` where `t` is an ISO DATE STRING ('2026-07-24'). `cohort_index.json` has keys companies/harvested_at/year; each company is {date ('Dec 30, 2022'), name, recovered, symbol, symbol_raw} — NO cik field.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/delisted_prices/2022/`)*
- Delisted archive coverage is 50 cohort rows per year for 2021-2026 with only a subset having price files (12/50 in 2021, 31/50 in 2022, 189 files in 2026). Its manifest license note: 'stockanalysis.com data: republication in full prohibited by site ToS; private research archive only. Derived values may be published with attribution.'  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/delisted_prices/2022/manifest.json`)*
- market_eod manifest license note is 'Massive (ex-Polygon) free-tier data: personal use; read current terms before any redistribution. Private archive.' — anything derived per-row from it must not land in the PUBLIC Stock-Grader repo (ECOSYSTEM rule 5).  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/2026-07/manifest.json`)*
- Stock-Grader's `.gitignore` already contains `build/` — a panel written under `build/` cannot be accidentally committed to the public repo. Stock-Grader remote is git@github.com:TylerJForstrom/Stock-Grader.git (public); Stock-Vault is private.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/.gitignore`)*
- Stock-Vault's `.gitattributes` sets `data/** -text`, so committed data bytes are byte-identical to what was sha256'd. Any file the builder writes into the vault must respect that.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/.gitattributes`)*
- Vault manifests are written by `write_manifest(dataset_dir, source_urls=, license_note=, extra=)` producing {schema_version:'1.0', generated_at_utc, source_urls (sorted), license_note, files:[{name, sha256, bytes}]}. `VaultDataSource` refuses any schema_version outside {'1.0'} and any file not listed.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/manifest.py:23-49 and src/stock_grader/data/vault.py:50-76`)*
- `monthly-freeze.yml` runs `cron: '19 13 1 * *'` and ends with a 3-attempt `git push` / `git pull --rebase origin main` loop. `collectors.yml` (vault EOD) runs `cron: '23 22 * * 1-5'` and gates itself on `stock-vault check-staleness --pre-collection`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/.github/workflows/monthly-freeze.yml and Stock-Vault/.github/workflows/collectors.yml`)*
- `paper-trader.yml` already demonstrates the cross-repo pattern: a second `actions/checkout@v4` with `repository: TylerJForstrom/Stock-Grader, path: grader`, then `stock-vault paper-rebalance --panel-source grader`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/.github/workflows/paper-trader.yml:19-40`)*
- `main()` reads shared flags only via `getattr(args, ..., default)`, so a subparser that omits them is safe; it wraps `args.func(args)` in a bare `except Exception` that prints and returns 1, so a gate failure must `return 2` rather than raise.  
  *(`src/stock_grader/cli.py:1209-1271`)*
- `profile_names()` returns 11 profiles: all_weather, deep_value, dividend_growth, dividend_income, garp, growth, low_volatility, momentum, quality, turnaround, value.  
  *(`src/stock_grader/profiles.py via profile_names()`)*
- `tests/test_vault.py` already provides `build_vault(root, corrupt=None)` which constructs a fixture vault with market_eod day files (AAPL, DEADCO, BRK.B), a borrow snapshot, and a delisted SIVB history, plus `_gz_jsonl` and `_manifest` helpers. Reuse it.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/tests/test_vault.py:16-72`)*
- CI runs `pytest -q --cov=stock_grader --cov-fail-under=55`; the two backtest CLI tests already redirect the ledger to `tmp_path/ledger.jsonl` specifically so runs cannot pollute the repo ledger.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/.github/workflows/ci.yml and tests/test_cli.py:340-343`)*

## Implementation steps

### Step 1. Decide and record placement: the builder lives in Stock-Grader, its output lands in Stock-Vault

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/docs/VALIDATION.md`

Argue and document this in `docs/VALIDATION.md` before writing code, because a later agent will otherwise move it. The one-direction DAG is sources -> foundry -> grader -> backtest -> forward (ECOSYSTEM.md rule 2). The joiner consumes a GRADER output (frozen_scores) and produces a BACKTEST input, so it sits strictly between grader and backtest — inside Stock-Grader. ECOSYSTEM.md also names Stock-Grader 'Home of the backtest evaluator and its attestations', and the attestations are exactly what this builder must decide honestly, so the code that decides them must live beside the evaluator that reads them (backtest.py). Putting it in Stock-Vault would invert the DAG (a source-side archive consuming grader panels to produce a grader input) and would fork return methodology out of the system of record. The read adapters it needs already live in Stock-Grader: `VaultDataSource` (local-clone-only by design) and `FoundryDataSource`. BUT: the built panel embeds per-row returns derived from Massive free-tier closes ('personal use ... Private archive'), and Stock-Grader is a PUBLIC repo, so under ECOSYSTEM rule 5 the panel file itself must never be committed there. Working copies go to `build/panels/` (already gitignored); the durable archive goes into the private vault at `data/backtest_panels/<profile>/`. Only aggregate statistics (the backtest markdown, the build accounting counts, the ledger line) are committed to Stock-Grader.

### Step 2. Create `src/stock_grader/panel.py` — trading-calendar and period selection

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/panel.py`

New module, no CLI, no network. Module docstring must state the licensing split and the total-return limitation up front.

Constants:
  `SCHEMA_VERSION = '1.0'`
  `FORWARD_EPOCH = dt.date(2026, 7, 30)`  # first genuinely forward-frozen panel on disk
  `PLAUSIBLE_SPLIT_RATIOS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0)`

`@dataclass(frozen=True, slots=True) PanelBuildConfig`: `horizon_days: int = 21`, `min_cross_section: int = 20`, `min_periods: int = 3`, `max_eod_lag_days: int = 5`, `max_freeze_age_days: int = 45`, `include_ungraded: bool = False`, `split_tolerance: float = 0.01`, `max_unresolved_fraction: float = 0.02`, `allow_backfilled_panels: bool = False`.

`discover_frozen_panels(frozen_root: Path, profile: str) -> dict[dt.date, Path]` — glob `frozen_root/<profile>/*.parquet`, accept only stems matching `^\d{4}-\d{2}-\d{2}$` (mirror `paper.py`'s `_PANEL_STEM` discipline). Return sorted by date. NOTE the legacy top-level `frozen_scores/*.parquet` layout is gone as of the multi-profile freeze; do not support it.

`trading_days(vault: VaultDataSource) -> list[dt.date]` — just `sorted(vault.market_eod_available_days())`. This IS the trading calendar; do NOT reimplement a holiday calendar (Stock-Vault has one in `staleness.py` but cross-repo imports are forbidden, and the archive's own coverage is the only calendar that matters for pricing).

`window_for(signal: dt.date, days: list[dt.date], horizon: int) -> tuple[dt.date, dt.date] | None` — entry `E` = first day in `days` STRICTLY greater than `signal` (satisfies backtest.py:141's strict inequality); exit `X` = the day `horizon` positions after `E` in `days`. Return None if either does not exist (the signal date has not matured).

`select_non_overlapping(signals: list[dt.date], days, horizon) -> tuple[list[dt.date], list[dt.date]]` — greedy earliest-first: keep a signal only if its entry `E` is at or after the previously kept signal's exit `X`. Returns (kept, skipped). Reason: an ad-hoc `workflow_dispatch` freeze mid-month would otherwise produce overlapping outcome windows, which double-counts the same market move and breaks the moving-block bootstrap's independence assumption and the `periods_per_year=12` annualization.

### Step 3. panel.py — bulk bar loader (must not use `market_eod_series`)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/panel.py`

`load_bars(vault, days: Iterable[dt.date], wanted: Mapping[str, str]) -> pd.DataFrame`
  `wanted` maps EVERY market_eod spelling variant (upper-cased) to its canonical panel ticker. Build it from `ticker_variants(t)` for each panel ticker; raise `PanelBuildError` if two panel tickers claim the same variant (a real collision would silently cross-join two issuers).
  For each day exactly once: `frame = vault.market_eod_day(day)`; filter `frame['symbol'].astype(str).str.upper().isin(wanted)`; append rows `{date, ticker (canonical), symbol (as archived), close, volume, transactions}`. Return a long DataFrame.
  PERFORMANCE CONTRACT, state it in the docstring: `VaultDataSource.market_eod_series` reads and sha256-verifies all 501 archived day files PER TICKER (vault.py:106-131). Reading each needed day file ONCE and filtering is ~250 file reads for a 12-period cumulative panel instead of ~41,000. Every day inside every retained window is needed (for the split scan), so cost grows linearly with retained periods; that is acceptable for a monthly job and is bounded by the free-tier's ~730-day archive.
  Guard: drop rows where `close` is null or <= 0 (verified 0 such rows on 2026-07-28, but the field is nullable in the source payload).

### Step 4. panel.py — split detection and correction (three tiers, never a silent guess)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/panel.py`

The archive is collected with `adjusted=false` (market_eod.py:40) so a split inside an outcome window fabricates a ~-50% or ~+900% return. Verified real: AMKL moved exactly -50.0% (11.75 -> 5.88) on 2026-07-28. The foundry's `splits.jsonl` has 2 rows (AAPL only), so it cannot be the sole defense.

`detect_split(prev_close, close, prev_volume, volume, prev_transactions, transactions, tolerance) -> float | None` — returns the candidate ratio when the PRICE signature matches a plausible split, else None. `ratio = prev_close / close`; try each `r` in PLAUSIBLE_SPLIT_RATIOS as a forward split (`abs(ratio/r - 1) <= tolerance`) and each `1/r` as a reverse split. Because the smallest plausible ratio is 1.5, this can only fire on a one-day move beyond about -33% or +50%.

`classify_split(candidate, foundry_splits, ticker, cik, day, bars...) -> tuple[float, str]`:
  Tier A `'foundry'`: a `splits()` row whose `ticker` (any variant) or `cik` matches and whose `effective_date == day` and whose `ratio` is within tolerance of the candidate -> use the foundry `ratio`.
  Tier B `'reconstructed'`: no foundry row, but the VOLUME signature corroborates — `volume/prev_volume` inside `[0.5*R, 2.0*R]` AND `transactions/prev_transactions` inside `[0.4, 2.5]` (a split multiplies share volume by R while leaving trade count roughly flat; a genuine -50% crash spikes BOTH). Use the candidate. Carries an honesty flag per ECOSYSTEM rule 6. Skip Tier B when `transactions` is null (verified 3 nulls per ~12.5k rows).
  Tier C `None`: price looks like a split, volume does not corroborate, foundry silent -> UNRESOLVED. The observation is excluded and counted; it is NOT silently kept (that would fabricate a -50% return) and NOT silently dropped (that would be a survivorship hole).

`split_factor(ticker_bars_in_window, ...) -> tuple[float, str, bool]` — walk consecutive archived days in `(E, X]`, multiply confirmed factors, return `(cumulative_factor, source in {'none','foundry','reconstructed','mixed'}, unresolved: bool)`. `forward_return = (close_X * factor) / close_E - 1`.

### Step 5. panel.py — delisting resolution chain (the survivorship guarantee)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/panel.py`

Survivorship bias enters exactly one way: dropping a frozen row because its outcome is inconvenient. The rule is that NO row is dropped for an outcome-dependent reason. Resolution order for a row whose canonical ticker has a close at `E` but none at `X`:
  1. `market_eod` close at `X` (any variant) -> `return_source='market_eod'`. Record the archived spelling actually matched in a `price_symbol` column; if the spelling at `X` differs from the one at `E`, keep the observation but flag it (`symbol_changed=True`) — ticker reuse is the known join hazard and CIK is not present in the EOD payload.
  2. `vault.delisted_history(v)` for each variant: SORT the index first (verified DESCENDING for AATC) and take the last row with `E < date <= X`; use column `c` (raw close), NOT `a` (the site's adjusted close, whose basis is undocumented). -> `return_source='delisted_archive'`, `delisting_resolved=True`.
  3. Last available `market_eod` close for any variant on a day in `(E, X]` — the final trade on a listed venue before the name left it. -> `return_source='last_listed_close'`, `terminal_price_used=True`, `delisting_resolved=True`. Document the convention explicitly: this holds the position at the last listed close and ignores any OTC continuation (market_eod is collected with `include_otc=false`), which slightly OVERSTATES the return for names that continued falling off-exchange. It is a disclosed convention, not a drop.
  4. Nothing found -> UNRESOLVED. Count it, exclude the row, and record it in the accounting. Any unresolved row anywhere in the panel forces the panel-level `delisting_return_included` attestation to False (backtest.py:167-176 admits no partial attestation).

Rows with no close at `E` are dropped as `no_start_price` — that is knowable at entry (you cannot buy an unpriced name) and is NOT outcome-dependent, so it does not break the PIT attestation. Count it separately anyway.
Rows with `graded == False` in the frozen file are excluded by default (`include_ungraded=False`). This is PIT-safe: `graded` was frozen before the outcome. Verified this currently removes BRK-B, UNH, V, XOM from the 82-row panel, leaving 78 (>= the backtest's default `min_cross_section` of 20).

### Step 6. panel.py — `build_panel()` and the honest attestations

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/panel.py`

`build_panel(frozen_root, profile, vault, foundry=None, config=PanelBuildConfig()) -> PanelBuildResult`.

Freshness gates FIRST (return a result carrying the failure; the CLI turns it into exit 2):
  - newest archived EOD day older than `today - max_eod_lag_days` -> refuse (a stale vault silently truncates maturity).
  - newest frozen signal date older than `today - max_freeze_age_days` -> refuse (a dead freeze clock must not look like 'no new evidence').
  - any frozen signal date < `FORWARD_EPOCH` -> refuse unless `allow_backfilled_panels`, in which case proceed but force `universe_is_pit = False`. Reason: `config/universe_default.txt` is a hand-picked large-cap list chosen in 2026; against a genuinely forward signal date it is PIT-honest (chosen before every outcome), but a `freeze --asof 2024-01-01` backfill against that same list is textbook survivorship. This gate is the only thing that stops a future agent from manufacturing fake history.

Emitted panel columns — exact names, in this order:
  REQUIRED by `_validate_panel`: `signal_date`, `return_start`, `return_end`, `ticker`, `score`, `forward_return` (ISO date strings; `forward_return` a finite decimal, never NaN — backtest.py:158-163 would silently drop NaNs).
  CONTRACT: `cik` (zero-padded 10-char string, non-null and non-blank on EVERY row or `permanent_identifier_present` fails; resolve a missing CIK from `FoundryDataSource.universe()` by ticker, and if still unresolved drop the row and count it), `filed_through` (pass through the frozen panel's column if it ever gains one; otherwise `= signal_date`, which is honestly the tightest bound available: the freeze fetched EDGAR ON signal_date, so no filing dated after signal_date could have entered the feature set — precisely the invariant backtest.py's module docstring asks for), `universe_is_pit` (bool), `return_is_total` (bool), `delisting_return_included` (bool).
  DIAGNOSTIC, ignored by the evaluator: `profile`, `letter`, `percentile`, `coverage`, `config_fingerprint`, `universe_fingerprint`, `freeze_commit`, `start_close`, `end_close`, `price_symbol`, `return_source`, `split_factor`, `split_source`, `terminal_price_used`, `symbol_changed`, `panel_schema_version`, `builder_commit` (from `research_manifest.current_commit()`).

Attestation rules — computed, never hard-coded True:
  `universe_is_pit = (outcome_dependent_drops == 0) and (min(signal_dates) >= FORWARD_EPOCH)`.
  `delisting_return_included = (unresolved_rows == 0)`.
  `return_is_total = False`, ALWAYS, in v1. Justify it in the docstring with the on-disk facts: `dividends.parquet` covers 3 tickers, its manifest declares `granularity: 'fiscal_period (no ex-dates in XBRL)'` and `basis: 'current_fully_split_adjusted'`, and market_eod closes are raw/unadjusted. Prorating a trailing fiscal DPS on the wrong split basis into a 21-day window and calling it a total return would be a lie the attestation column exists to prevent. Write the exact upgrade condition in the docstring: flip to True only when a per-ex-date cash-dividend dataset (ex_date, cash_amount, on the same unadjusted basis) covers >= 99% of panel rows. DO NOT add a `--attest-total-return` flag; there must be no switch that makes the panel claim something untrue.

Also emit a `dividend_yield_estimate` column ONLY if `--foundry` is supplied AND you deliberately choose to; if you do, it is a labelled diagnostic and must NEVER be folded into `forward_return`. Preferred: omit it entirely in v1.

Per-period accounting dataclass `PeriodAccounting` with fields: `signal_date`, `return_start`, `return_end`, `frozen_rows`, `ungraded_dropped`, `missing_cik_dropped`, `no_start_price_dropped`, `resolved_market_eod`, `resolved_delisted_archive`, `resolved_last_listed_close`, `split_adjusted_foundry`, `split_adjusted_reconstructed`, `unresolved_dropped`, `kept`, `meets_min_cross_section`.

Hard checks before returning: no duplicate `(signal_date, ticker)` and no duplicate `(signal_date, cik)` — raise `PanelBuildError` with the offending pairs named. A future universe containing GOOG and GOOGL would otherwise hit backtest.py:151-157's opaque 'duplicate signal_date/security observations' error.

### Step 7. panel.py — writers: parquet + sidecar JSON + a vault-shaped manifest

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/panel.py`

`write_panel(result, out_dir: Path, profile: str) -> tuple[Path, Path]` writes `out_dir/<profile>.parquet` (STABLE name — see the next step; the ledger's `experiment` key is derived from the panel filename at cli.py:818) and `out_dir/<profile>.build.json`.

Sidecar JSON — top-level keys, flat (never wrap an existing documented schema in an envelope; HANDOFF working rule 2): `schema_version` ('1.0'), `profile`, `built_utc`, `builder_commit`, `horizon_days`, `matured_signal_dates`, `pending_signal_dates`, `skipped_overlapping_signal_dates`, `periods` (list of PeriodAccounting dicts), `qualifying_periods` (count with `meets_min_cross_section`), `attestations` (the three booleans), `unresolved_rows`, `unresolved_fraction`, `ready_for_backtest` (bool). `ready_for_backtest = qualifying_periods >= config.min_periods`.

`archive_to_vault(panel_path, sidecar_path, archive_dir: Path, profile: str, build_date) -> Path` copies the parquet to `archive_dir/<profile>/<YYYY-MM-DD>.parquet` and rewrites `archive_dir/<profile>/manifest.json`. Copy the ~25-line manifest writer rather than importing `stock_vault.manifest` (ECOSYSTEM rule 1, and the precedent already set for `ticker_variants` in HANDOFF item 4). It must produce exactly `{schema_version: '1.0', generated_at_utc: '%Y-%m-%dT%H:%M:%SZ', source_urls: sorted([...]), license_note: <string>, files: [{name, sha256, bytes}]}` over every non-dot, non-manifest file in the directory, or `VaultDataSource._manifest`/`_read_verified` will refuse it. license_note must name both upstreams: Massive free-tier EOD closes and stockanalysis.com delisted histories, private archive, do not redistribute rows. Write bytes (parquet is binary; the vault's `.gitattributes` sets `data/** -text` so the committed bytes match the hash).

### Step 8. Add the `build-panel` subcommand to cli.py

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py`

`cmd_build_panel(args) -> int` plus registration in `build_parser()`. Register EVERY flag on the SUBPARSER `p_build_panel` — do NOT call `common(...)` (build-panel grades nothing and must not inherit --price-provider etc.). HANDOFF working rule 3: a top-level-only flag that a workflow passes subcommand-first silently kills every scheduled run.

Flags: `--profile` (required, `choices=profile_names()`), `--frozen-root` (default `frozen_scores`), `--vault` (REQUIRED, local clone path only — `VaultDataSource` is local-clone-only by design, vault.py:1-12; do not accept a URL), `--foundry` (optional local path or raw URL, used ONLY for split confirmation), `--out` (default `build/panels`), `--archive-dir` (optional; when set, copy into the vault archive), `--horizon-days` (default 21), `--min-cross-section` (default 20, mirroring `BacktestConfig`), `--min-periods` (default 3), `--max-eod-lag-days` (default 5), `--max-freeze-age-days` (default 45), `--max-unresolved-fraction` (default 0.02), `--include-ungraded` (store_true), `--allow-backfilled-panels` (store_true), `--no-verify-hashes` (store_true, passed to `VaultDataSource(verify_hashes=False)` for local iteration only), `--format` (text|json, default text).

Behaviour and exit codes — `main()` swallows exceptions into exit 1 (cli.py:1265-1270), so use RETURNS not raises for gates:
  0 matured signal dates -> write the sidecar with `periods: []`, `ready_for_backtest: false`, print 'no frozen panel has matured yet (needs a signal date whose entry day + N trading days are archived)', **return 0**. A red job every month for a structurally expected state trains the owner to ignore the failure email — which is the only alerting this ecosystem has.
  freshness or backfill gate fails -> print the reason, **return 2**.
  `unresolved_fraction > --max-unresolved-fraction` -> print the affected tickers, **return 2**.
  otherwise write panel + sidecar (+ archive), print a one-line summary and the per-period accounting table, **return 0**.

`--format json` prints the sidecar payload so the workflow can read `ready_for_backtest` without a second file read.

### Step 9. Ledger hygiene part 1: retraction records

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/research_manifest.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/research_ledger.jsonl`

The live `research_ledger.jsonl` holds 12 records, all `experiment='backtest:panel.csv'` with `per_period_sharpe = 2.8284271247461894` — synthetic CLI-test panels (`score=index, forward_return=index/1000`), not market hypotheses. `deflated_sharpe_ratio` benchmarks against `expected_max_sharpe(stdev(trial_sharpes), n)` (significance.py:170-183), so once real trials (Sharpe ~0.2-0.5) join those 2.83s the dispersion explodes and the deflation benchmark makes any real edge permanently undetectable. The ledger is append-only and hash-chained, so the fix must be an APPEND.

In `research_manifest.py` add (and to `__all__`):
  `RETRACTION_EXPERIMENT = 'ledger:retraction'`
  `def retracted_hashes(records) -> set[str]` — union of `record['symbols']` over records whose `experiment == RETRACTION_EXPERIMENT`. (`symbols` is `Sequence[str]` and is serialized verbatim by `payload()`, so it is the natural carrier; the retraction is itself hashed and chained, so what was excluded and why is permanently auditable.)
  `def trial_sharpes(records) -> list[float]` — see the next step.

In `cli.py` add `cmd_ledger_retract` and subparser `ledger-retract`: positional `sha256` (nargs='+'), `--ledger` (default `research_ledger.jsonl`), `--reason` (required). It must refuse a hash absent from the ledger, refuse to retract a retraction record, and refuse if `verify_chain` is already False. It appends `ResearchRecord(experiment=RETRACTION_EXPERIMENT, market='us_equities', symbols=<hashes>, targets=[], horizons=[], trials=0, metrics={}, costs={}, benchmark='none', leakage_controls='n/a', gate_passed=False, verdict=<reason>, code_commit=current_commit())`.

Then perform the one-time operational action: retract all 12 with reason 'synthetic CLI-test panel (score=index, forward_return=index/1000); a unit-test fixture is not a strategy searched'. Commit the appended line.

### Step 10. Ledger hygiene part 2: one hypothesis = one trial, not one trial per month

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/research_manifest.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py`

SURGICAL edit — `cmd_backtest` is being modified concurrently by another agent (benchmark/fill journaling); rebase immediately before, and change nothing else in that function.

Add to `research_manifest.py`:
```
def trial_sharpes(records: Sequence[dict[str, object]]) -> list[float]:
    """Per-period Sharpes of the DISTINCT configurations searched.

    Retracted records are excluded. The remainder are collapsed to the most
    recent entry per ``experiment``: re-running one profile every month on a
    longer sample is that trial measured again, not a new trial, and counting
    it twelve times a year inflates the deflation benchmark until no real
    edge could ever clear it.
    """
```
Implementation: walk in file order, skip records whose `integrity_sha256` is in `retracted_hashes(records)` and records whose `experiment == RETRACTION_EXPERIMENT`; keep `latest[experiment] = metrics['per_period_sharpe']` (overwriting); return the finite float values. Legacy records with distinct experiment names collapse to themselves, so this is backward compatible.

In `cmd_backtest`, replace ONLY the list comprehension at cli.py:795-801 with `trial_sharpes = trial_sharpes_from(prior)` (import it alongside the existing `ResearchRecord, append_record, current_commit, load_manifest` import at cli.py:781-786). Everything downstream — the `math.isfinite(this_sharpe)` append, `assess_edge(...)`, the record's `trials=len(trial_sharpes)`, the `lifetime_trials` payload key — is unchanged.

Also add, in the same record's `metrics`, `'sequential_looks': float(number of prior non-retracted records sharing this experiment) + 1.0`. Repeated monthly measurement of one hypothesis is still optional-stopping, and the honest thing is to make the look count visible in the ledger even though the configuration count is now correctly deduplicated. Document this in `docs/VALIDATION.md`.

### Step 11. Add `.github/workflows/monthly-forward-backtest.yml`

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/.github/workflows/monthly-forward-backtest.yml`

Lives in Stock-Grader because the ledger and the report — the primary outputs — are committed there without a token. One new repo secret `VAULT_REPO_TOKEN` (fine-grained PAT, Contents: read+write on TylerJForstrom/Stock-Vault) covers both reading the private archive and pushing the panel archive back.

```yaml
name: monthly-forward-backtest
on:
  schedule:
    - cron: "41 2 6 * *"   # 6th, 02:41 UTC: after the 5th's vault collector commit (23 22 * * 1-5)
  workflow_dispatch:
    inputs:
      profiles: { description: "space-separated profiles (blank = every frozen profile)", default: "" }
permissions:
  contents: write
concurrency:
  group: monthly-forward-backtest
```
Steps:
 1. `actions/checkout@v4` (self).
 2. `actions/checkout@v4` with `repository: TylerJForstrom/Stock-Vault`, `token: ${{ secrets.VAULT_REPO_TOKEN }}`, `path: vault` — exactly the cross-repo pattern paper-trader.yml already uses.
 3. `actions/checkout@v4` with `repository: TylerJForstrom/Stock-Data`, `path: foundry` (public, no token).
 4. `actions/setup-python@v5` 3.12; `pip install -e .`.
 5. GATE, before anything writes: refuse to append to a broken chain —
    `python -c "import sys;from stock_grader.research_manifest import load_manifest,summarize_manifest;s=summarize_manifest(load_manifest('research_ledger.jsonl'));print(s);sys.exit(0 if s['all_integrity_ok'] else 1)"`.
 6. Build + evaluate, per profile, with `set -uo pipefail` (NOT `-e` — one refusing profile must not suppress the others, mirroring cmd_freeze's `refused` list):
```bash
MONTH=$(date -u +%Y-%m); OUT="docs/forward/$MONTH"; mkdir -p build/panels "$OUT"
PROFILES="${{ github.event.inputs.profiles }}"
[ -z "$PROFILES" ] && PROFILES=$(ls frozen_scores)
failed=0
for p in $PROFILES; do
  stock-grader build-panel --profile "$p" --frozen-root frozen_scores \
    --vault vault --foundry foundry --out build/panels \
    --archive-dir vault/data/backtest_panels \
    --horizon-days 21 --min-cross-section 20 --min-periods 3 || { echo "::error::build-panel failed for $p"; failed=1; continue; }
  ready=$(python -c "import json;print(json.load(open('build/panels/$p.build.json'))['ready_for_backtest'])")
  if [ "$ready" != "True" ]; then echo "::notice::$p not ready for backtest yet"; continue; fi
  stock-grader backtest "build/panels/$p.parquet" --ledger research_ledger.jsonl \
    --periods-per-year 12 --min-cross-section 20 --quantiles 5 \
    --allow-unverified-panel --format md > "$OUT/$p.md" || { echo "::error::backtest failed for $p"; failed=1; continue; }
  cp "build/panels/$p.build.json" "$OUT/$p.build.json"
done
exit $failed
```
 `--allow-unverified-panel` is MANDATORY and not a shortcut: `return_is_total` is honestly False, so `cmd_backtest` (cli.py:766-775) would otherwise refuse every run. The resulting ledger line records `leakage_controls: 'panel attestation contract: FAILED total_returns_attested'` — which is the true state of the evidence.
 7. Commit to the vault (`if: always()`): `git -C vault add -A data/backtest_panels`, skip if `git -C vault diff --cached --quiet`, commit `panels: $(date -u +%Y-%m)`, then the same 3-attempt `git push` / `git pull --rebase origin main` loop as monthly-freeze.yml.
 8. Commit to Stock-Grader (`if: always()`): `git add -A research_ledger.jsonl docs/forward`, same skip-if-empty and 3-attempt rebase-retry loop. NEVER `git add build/` — it is gitignored, which is the structural guarantee that Massive-derived rows cannot reach the public repo.

Staleness alerting: the freshness gates live inside `build-panel` (exit 2), so a stale vault or a dead freeze clock turns the scheduled run red and GitHub emails the owner — the same 'the failure email IS the alerting' pattern collectors.yml uses.

### Step 12. Tests — `tests/test_panel.py` (new) plus additions to test_cli.py and test_research_manifest.py

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/tests/test_panel.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/tests/test_cli.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/tests/test_research_manifest.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/tests/test_vault.py`

Reuse `tests/test_vault.py::build_vault` (import it, or refactor it into a shared fixture) — it already builds a manifest-verified vault with AAPL, DEADCO (present day 1, gone day 2), BRK.B, and a delisted SIVB history. Extend the fixture with enough day files to span a horizon, a split-shaped name, and a frozen_scores tree.

Required tests (names are the acceptance contract):
  `test_built_panel_passes_the_backtest_input_contract_except_total_returns` — `evaluate_walk_forward(pd.read_parquet(panel))` returns `input_contract == {filing_cutoff_provided: True, point_in_time_universe_attested: True, total_returns_attested: False, delistings_included_attested: True, permanent_identifier_present: True}` and `'does not attest that forward_return includes distributions'` appears in `report.limitations`.
  `test_return_start_is_strictly_after_signal_date_and_windows_are_uniform` — no ValueError from `_validate_panel`; each signal_date has exactly one `(return_start, return_end)` pair.
  `test_delisted_name_is_priced_not_dropped` — DEADCO is in the frozen panel, vanishes from market_eod at the exit day, and appears in the built panel with `return_source in {'delisted_archive','last_listed_close'}`; the row count equals the graded frozen row count.
  `test_missing_terminal_price_forces_delisting_attestation_false` — an unresolvable vanished name yields `delisting_return_included == False` for the whole panel and a non-zero `unresolved_rows`.
  `test_split_confirmed_by_foundry_is_divided_out` — a fixture 2-for-1 with a matching `splits.jsonl` row gives a forward return near the true economic return, `split_source == 'foundry'`.
  `test_split_reconstructed_from_volume_signature` — same halving, no foundry row, volume x2 and transactions flat -> corrected, `split_source == 'reconstructed'`.
  `test_uncorroborated_halving_is_excluded_not_guessed` — halving with transactions x20 (a crash-shaped signature) -> the row is excluded and counted in `unresolved_dropped`, and the build returns 2 when it exceeds `--max-unresolved-fraction`.
  `test_sec_dash_ticker_finds_polygon_dot_symbol` — a frozen `BRK-B` row prices against the archived `BRK.B` bar (the verified real-world failure mode).
  `test_ungraded_rows_excluded_by_default_and_counted`.
  `test_backfilled_signal_date_before_forward_epoch_is_refused` — and, with `--allow-backfilled-panels`, `universe_is_pit` is forced False.
  `test_stale_eod_archive_refuses_to_build` and `test_stale_freeze_clock_refuses_to_build` — exit 2.
  `test_zero_matured_panels_writes_sidecar_and_exits_zero` — `ready_for_backtest is False`, `periods == []`, exit code 0, no parquet written.
  `test_fewer_than_min_periods_writes_panel_but_is_not_ready` — 2 qualifying periods -> parquet exists, `ready_for_backtest is False`.
  `test_duplicate_cik_in_one_signal_date_is_refused_with_names` — a GOOG/GOOGL-style pair raises `PanelBuildError` naming both tickers, not the opaque backtest error.
  In `tests/test_research_manifest.py`: `test_retraction_excludes_named_records_from_trial_sharpes`, `test_trial_sharpes_collapse_repeated_measurements_of_one_experiment` (12 identical `backtest:panel.csv` records -> 1 trial), `test_retract_refuses_unknown_hash_and_broken_chain`.
  In `tests/test_cli.py`: `test_build_panel_json_format_reports_readiness`, `test_ledger_retract_appends_and_keeps_chain_valid`.

EVERY test that touches a ledger MUST pass `--ledger tmp_path/ledger.jsonl` — the repo ledger already carries 12 records of exactly this mistake.

### Step 13. Documentation and handoff bookkeeping

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/docs/VALIDATION.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/LIMITATIONS.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/HANDOFF.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/REVIEW_FEEDBACK.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/README.md`, `C:/Users/tforstrom/Desktop/Stock-Data/ECOSYSTEM.md`

`docs/VALIDATION.md`: new section 'Building the forward panel' after '## Minimum credible dataset construction'. State (a) the placement argument from step 1, (b) the exact emitted column list, (c) how each of the five contract items is satisfied or honestly refused — in particular that `return_is_total` is False and WHY (3-ticker dividend coverage, fiscal-period granularity, no ex-dates, split-basis mismatch against raw closes) and the exact condition for flipping it, (d) the delisting resolution order and the 'last listed close' convention with its known overstatement, (e) the split tiers, (f) one-hypothesis-one-trial and the `sequential_looks` caveat, (g) why fewer than 3 qualifying periods appends no trial.
`docs/LIMITATIONS.md`: add the price-only-return, OTC-continuation, and reconstructed-split caveats.
`docs/HANDOFF.md`: mark queue item 10's first bullet ('§6 panel builder') done with the commit hash; note that item 5 (events.jsonl manifest) is already satisfied on disk and that item 6 (delisted cohort CIK linkage) would upgrade the delisting chain from symbol matching to CIK matching.
`docs/REVIEW_FEEDBACK.md`: one line per completed chunk under 'Agent log', per AGENTS.md.
`README.md`: add `build-panel` and `ledger-retract` to the command list.
Also record in Stock-Data's `ECOSYSTEM.md` decision log that derived backtest panels live in the private vault under `data/backtest_panels/` per licensing rule 5.

### Step 14. Local end-to-end dry run before the first scheduled execution

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/build/panels`

Today there is exactly ONE frozen panel (`frozen_scores/all_weather/2026-07-30.parquet`) and the newest archived EOD day is 2026-07-28, so the entry day (first archived day after 2026-07-30) does not exist yet: the correct local result is ZERO matured periods, a sidecar with `ready_for_backtest: false`, and exit 0. Verify exactly that:
`python -m stock_grader.cli build-panel --profile all_weather --vault C:/Users/tforstrom/Desktop/Stock-Vault --foundry C:/Users/tforstrom/Desktop/Stock-Data --out build/panels --format json`
Then prove the happy path on synthetic-but-real-shaped inputs: write two extra fixture frozen panels dated inside the archived EOD range (e.g. 2026-04-01, 2026-05-01, 2026-06-01) into a tmp `--frozen-root`, run against the REAL vault, and confirm a 3-period panel builds, `evaluate_walk_forward` accepts it, and `stock-grader backtest <panel> --ledger <tmp>/ledger.jsonl --allow-unverified-panel --format md` produces a report. Do this against a scratch ledger only. Do NOT commit those fixture frozen panels — a backfilled panel dated before FORWARD_EPOCH must never enter `frozen_scores/`.
Finally, per HANDOFF working rule 5, after enabling the workflow dispatch it once and confirm with `gh run list --repo TylerJForstrom/Stock-Grader --limit 5` that it actually completed.

## Pitfalls

- `VaultDataSource.market_eod_series()` (vault.py:106-131) reads AND sha256-verifies all 501 archived day files per ticker. Calling it once per panel ticker is ~41,000 file reads. Read each needed day file once with `market_eod_day()` and pivot.
- The frozen panel uses SEC dash form and market_eod uses Polygon dot form. Verified: `BRK-B` is absent from the 2026-06-30 EOD file, `BRK.B` is present. Every price lookup must go through `ticker_variants()`; raise if two panel tickers map to the same archived symbol.
- market_eod is collected with `adjusted=false` (market_eod.py:40), so raw prices. Verified real split artifact: AMKL moved exactly -50.0% on 2026-07-28. The foundry `splits.jsonl` has 2 rows (AAPL only) and cannot be the sole defense — hence the three-tier detector. Never keep an uncorroborated halving as a real return, and never silently drop it either.
- `dividends.parquet` covers 3 tickers and its manifest states `granularity: 'fiscal_period (no ex-dates in XBRL)'` and `basis: 'current_fully_split_adjusted'`. Prorating trailing DPS into forward_return against raw unadjusted closes would be dishonest on two axes (no ex-date, wrong split basis). `return_is_total` must be False and there must be no flag that flips it.
- `_attested` (backtest.py:167-176) requires the column present, non-null, and truthy on EVERY row — there is no partial attestation. Panel-level attestations must be uniform; per-row honesty belongs in separate ignored columns (`return_source`, `split_source`, `terminal_price_used`).
- `_permanent_id_column` (backtest.py:179-185) requires `cik` non-null AND non-blank on EVERY row. One missing CIK silently fails `permanent_identifier_present` for the whole panel and costs the turnover-across-ticker-changes property.
- `_validate_panel` silently DROPS rows with non-finite `score` or `forward_return` (backtest.py:158-163). Never emit NaN for an unresolved row — exclude it explicitly and count it, or the drop becomes invisible survivorship.
- `_validate_panel` raises on duplicate `(signal_date, cik)`. Today's 82-name universe has 82 distinct CIKs, but adding a dual-class pair (GOOG + GOOGL) would raise an opaque error. Detect and fail with the offending tickers named.
- `evaluate_walk_forward` raises when NO signal_date clears `min_cross_section` (backtest.py:326-329). Gate the workflow on the builder's own count of periods that meet the min, not on the raw matured count.
- `cmd_backtest` refuses any panel failing the strict contract unless `--allow-unverified-panel` (cli.py:766-775). A price-only panel ALWAYS fails `total_returns_attested`, so the flag is mandatory in the workflow — and the ledger honestly records `FAILED total_returns_attested` forever.
- The live `research_ledger.jsonl` already holds 12 synthetic `backtest:panel.csv` trials at Sharpe 2.8284. They currently do no damage (identical values -> stdev 0 -> benchmark 0), but the instant one real trial lands the dispersion explodes and `expected_max_sharpe` makes any true edge undetectable. The ledger is hash-chained and append-only: the fix is a retraction record, never a delete.
- Re-running one profile monthly is one hypothesis measured repeatedly, not twelve trials. Without the collapse-by-experiment fix, 11 profiles x 12 months = 132 trials/year of deflation against an 11-configuration search. Over-deflation is fail-safe (it cannot manufacture a false EDGE) but it destroys all power, which is the point of the exercise.
- `assess_edge` structurally cannot return significant below 11 periods: `block_bootstrap_sharpe_ci` returns `(0.0, 0.0)` when `n < block + 1` (block default 10) and `significant` requires `ci_lo > 0.0`. It reports 'INSUFFICIENT SAMPLE' below 30. Appending a ledger trial for a 1-2 period backtest burns a permanent trial on a statistic that cannot mean anything.
- CLI args that a workflow passes subcommand-first MUST be registered on the subparser, not the top-level parser (HANDOFF working rule 3 — this exact mistake silently killed every scheduled run in two repos).
- `main()` wraps `args.func(args)` in a bare `except Exception` that prints and returns 1 (cli.py:1265-1270). Gate failures must `return 2`, not raise, if the workflow is to distinguish 'refused on purpose' from 'crashed'.
- Stock-Grader is PUBLIC; per-row returns derived from Massive free-tier closes are restricted (ECOSYSTEM rule 5). Write working panels to `build/` (already gitignored — that is the structural guarantee) and archive only into the private vault. Never `git add build/`.
- Stock-Vault's `.gitattributes` sets `data/** -text` precisely so manifest sha256s describe the committed bytes. Write parquet and manifest as bytes and hash exactly what is written.
- `delisted_history()` returns a DESCENDING date index and `t` as an ISO date STRING (verified for AATC); it also searches year directories in reverse and returns the FIRST match, so a symbol appearing in multiple cohorts resolves to the newest. Sort before taking a terminal price, and bound the search to the outcome window.
- `cohort_index.json` carries no `cik` (HANDOFF item 6 is still open), so delisting resolution is symbol-only and ticker reuse can mis-join. Bound every lookup to the window and record `return_source` so a bad join is auditable rather than invisible.
- market_eod rows carry NO date field — the date lives only in the filename (market_eod.py:62-65). And `include_otc=false`, so a name that moves to OTC vanishes rather than continuing; that is the reason the 'last listed close' convention exists and must be disclosed as an overstatement.
- `config/universe_default.txt` is a hand-picked list chosen in 2026. It is PIT-honest against forward signal dates but pure survivorship against backfilled ones. The FORWARD_EPOCH refusal is the only thing preventing a future `freeze --asof 2024-01-01` from manufacturing fake history that looks contract-clean.
- `src/stock_grader/cli.py` is being edited RIGHT NOW by another agent (multi-profile freeze; the vault's `paper.py` is gaining benchmark/fill journal record kinds). Rebase immediately before touching `cmd_backtest`, keep that edit to the single list comprehension at cli.py:795-801, and note that `paper.py::load_frozen_panel` still globs top-level `frozen_scores/*.parquet` and has not yet been moved to the per-profile layout.
- Any test that runs `cmd_backtest` must pass `--ledger tmp_path/...`. CI enforces `--cov-fail-under=55`, so a new module without tests drops the ratchet and reds the build.
- Zero matured panels is the CORRECT state for months (first frozen panel is 2026-07-30; with a 21-trading-day horizon the third qualifying period arrives around early October 2026). Failing the job in that state trains the owner to ignore the failure email, which is the only alerting this ecosystem has.

## Acceptance criteria

Do not report this milestone complete until every box is checkable.

- [ ] `pytest -q` passes with 0 failures both before and after each commit; the count is at least the pre-change baseline plus the ~18 new tests, and `--cov-fail-under=55` still passes.
- [ ] `tests/test_panel.py::test_built_panel_passes_the_backtest_input_contract_except_total_returns` asserts `evaluate_walk_forward(...).input_contract == {'filing_cutoff_provided': True, 'point_in_time_universe_attested': True, 'total_returns_attested': False, 'delistings_included_attested': True, 'permanent_identifier_present': True}`.
- [ ] `tests/test_panel.py::test_delisted_name_is_priced_not_dropped` passes: the built panel's row count for a period equals that period's graded frozen row count, and the vanished name carries `return_source` in {'delisted_archive', 'last_listed_close'}.
- [ ] `tests/test_panel.py::test_uncorroborated_halving_is_excluded_not_guessed` and `test_split_reconstructed_from_volume_signature` and `test_split_confirmed_by_foundry_is_divided_out` all pass.
- [ ] `tests/test_panel.py::test_sec_dash_ticker_finds_polygon_dot_symbol` passes (a frozen `BRK-B` row prices against an archived `BRK.B` bar).
- [ ] `tests/test_panel.py::test_zero_matured_panels_writes_sidecar_and_exits_zero` passes: exit code 0, `build/panels/<profile>.build.json` exists with `ready_for_backtest == false` and `periods == []`, and NO `<profile>.parquet` was written.
- [ ] `tests/test_panel.py::test_fewer_than_min_periods_writes_panel_but_is_not_ready` passes: parquet exists, `ready_for_backtest == false`, and the workflow's readiness expression would skip the backtest.
- [ ] `tests/test_panel.py::test_backfilled_signal_date_before_forward_epoch_is_refused` passes and, with `--allow-backfilled-panels`, `universe_is_pit` is False in the emitted panel.
- [ ] `tests/test_research_manifest.py::test_trial_sharpes_collapse_repeated_measurements_of_one_experiment` passes: 12 records sharing one `experiment` yield exactly 1 trial Sharpe.
- [ ] `tests/test_research_manifest.py::test_retraction_excludes_named_records_from_trial_sharpes` passes and `verify_chain` still returns True over the ledger containing the retraction.
- [ ] On disk: `research_ledger.jsonl` has grown by exactly one line whose `experiment` is `ledger:retraction`, whose `symbols` lists the 12 `backtest:panel.csv` integrity hashes; `python -c "from stock_grader.research_manifest import *; print(summarize_manifest(load_manifest('research_ledger.jsonl')))"` reports `all_integrity_ok: True, chain_ok: True`, and `trial_sharpes(load_manifest('research_ledger.jsonl')) == []`.
- [ ] Real-data smoke test: `python -m stock_grader.cli build-panel --profile all_weather --vault C:/Users/tforstrom/Desktop/Stock-Vault --foundry C:/Users/tforstrom/Desktop/Stock-Data --out build/panels --format json` exits 0 and prints `"ready_for_backtest": false` with `"matured_signal_dates": []` (correct today: the only frozen date is 2026-07-30 and the archive ends 2026-07-28).
- [ ] Real-data happy path: with three fixture frozen panels dated inside the archived EOD range under a tmp `--frozen-root`, `build-panel` exits 0, `ready_for_backtest` is true, and `stock-grader backtest build/panels/all_weather.parquet --ledger <tmp>/ledger.jsonl --periods-per-year 12 --allow-unverified-panel --format md` exits 0 and emits a report whose 'Input contract' table shows total-returns as NO and the other four as yes.
- [ ] `git status --porcelain` shows nothing under `build/` staged or untracked-but-tracked; `git check-ignore -v build/panels/all_weather.parquet` confirms the panel is ignored in Stock-Grader.
- [ ] `.github/workflows/monthly-forward-backtest.yml` exists with `cron: "41 2 6 * *"`, `workflow_dispatch`, `permissions: contents: write`, a `concurrency.group`, three checkouts (self, Stock-Vault with `token: ${{ secrets.VAULT_REPO_TOKEN }}`, Stock-Data), a ledger-chain gate before any write, `--allow-unverified-panel` on the backtest invocation, and two independent 3-attempt push/`git pull --rebase origin main` commit loops.
- [ ] `stock-grader --help` lists `build-panel` and `ledger-retract`; `stock-grader build-panel --help` shows every flag registered on the subparser (verify `stock-grader build-panel --profile all_weather --vault X` parses, i.e. subcommand-first argument order works).
- [ ] `docs/VALIDATION.md` contains a 'Building the forward panel' section naming the exact emitted columns, the reason `return_is_total` is False plus the exact condition to flip it, the delisting resolution order, the three split tiers, and the one-hypothesis-one-trial rule; `docs/HANDOFF.md` queue item 10's panel-builder bullet is struck with a commit hash.
- [ ] After enabling: `gh run list --repo TylerJForstrom/Stock-Grader --limit 5` shows one completed (not merely dispatched) `monthly-forward-backtest` run.
