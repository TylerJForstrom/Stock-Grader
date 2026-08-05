# M2 — Signal-decay measurement: multi-horizon rank-IC curve over frozen panels (`stock-grader decay`)

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

**Why it matters:** Today the whole ecosystem assumes a monthly holding period because `freeze` runs monthly and `BacktestConfig.periods_per_year` defaults to 12 — nobody has measured whether the score's information actually lives 5, 21, 63, or 126 trading days. M2 builds one backtest-shaped panel per horizon from the same frozen scores, runs the existing `evaluate_walk_forward` evaluator on each, and reports a rank-IC decay curve (mean IC, IC information ratio, IC/sqrt(h), half-life) so the holding period is chosen from evidence instead of convention. Because a horizon sweep is N extra looks at the same data, every horizon is charged as its own ledger trial against the shared deflated-Sharpe denominator, and only a pre-declared primary horizon may pass the gate — the sweep buys knowledge about where the edge is without buying a false claim that it exists.

## Prerequisites

- At least 2 frozen signal dates under frozen_scores/<profile>/ — today only frozen_scores/2026-07-30.parquet exists (and at the legacy flat path). Either wait for monthly-freeze.yml to accrue dates, or back-freeze with `stock-grader freeze --asof <D> --pit --out retro_scores`.
- Vault market_eod archive extending at least `max(horizons)` archived sessions PAST the newest signal date. Archive currently ends 2026-07-28 (501 sessions from 2024-07-29); 126d beyond a 2026-07-30 panel needs data through roughly 2027-02.
- The multi-profile freeze (frozen_scores/<profile>/YYYY-MM-DD.parquet) — VERIFIED LANDED in cli.py:904-907; no longer a blocker.
- The significance/ledger wiring in cmd_backtest — VERIFIED LANDED at cli.py:776-857; decay reuses its ledger path and denominator convention.
- HANDOFF item 4 (ticker canonicalization: canonical = SEC dash form, ticker_variants extended with the space form) is NOT done. market_eod_close_matrix must build its own alias map from ticker_variants plus the space form until item 4 lands, then be simplified to use the shared helper.
- Independent of the paper-trader work in Stock-Vault/src/stock_vault/paper.py. Its journal currently emits only {"kind":"rebalance"} and {"kind":"snapshot"}; the pending {"kind":"benchmark"} / {"kind":"fill"} additions are the forward-layer's realized-return record and are a natural M3 consumer of the horizon this milestone selects, but M2 must not read the journal.

## Verified ground truth

Every line below was confirmed by reading the cited code or data file. Re-verify anything that looks
stale before relying on it — and if the code contradicts this document, the code wins: say so in your
report rather than bending the code to match the doc.

- backtest.py FORBIDS mixing horizons inside one panel, per signal_date. Verbatim: comment `# One signal date must correspond to one outcome window.  Mixing horizons makes a mean` / `# spread uninterpretable and can overlap training/test outcomes in a later model fit.` then `starts = group["return_start"].drop_duplicates()`, `ends = group["return_end"].drop_duplicates()`, `if len(starts) != 1 or len(ends) != 1: raise ValueError(f"signal_date {signal_date.date()} mixes return windows")`. This is checked inside the `for signal_date, group in frame.groupby("signal_date", sort=True)` loop, so the rule is one window per signal_date — which for a sweep over the same signal dates means one physical panel file per horizon.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:281-286`)*
- A second, harder guard makes stacking horizons in one file structurally impossible: `duplicate_ticker = frame.duplicated(["signal_date", "ticker"])` and `duplicate_security = frame.duplicated(["signal_date", "_security_key"])` both raise `ValueError("duplicate signal_date/security observations are not allowed (checked both ticker and permanent identifier)")`. Two horizons for the same (signal_date, ticker) are duplicates by definition.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:151-157`)*
- Window chronology is enforced: `if (frame["return_start"] <= frame["signal_date"]).any(): raise ValueError("every return_start must be strictly after signal_date")` and `if (frame["return_end"] <= frame["return_start"]).any(): raise ValueError("every return_end must be strictly after return_start")`. Entry must therefore be a session strictly after signal_date.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:141-144`)*
- Rank-IC IS computed today, in exactly one production place: `rank_ic = float(group["score"].corr(group["forward_return"], method="spearman"))` per signal_date group. It is aggregated into `BacktestReport.mean_rank_ic`, `.rank_ic_information_ratio`, `.rank_ic_positive_rate`, `.rank_ic_interval` (moving-block bootstrap). The IR is `float(np.mean(rank_ics) / ic_std * math.sqrt(config.periods_per_year))` with `ic_std = float(np.std(rank_ics, ddof=1))`, returning None when ic_std == 0. Nothing new needs to be written to compute rank IC — the decay curve is one `evaluate_walk_forward` call per horizon panel.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:288, 330, 344-349, 387-403`)*
- A SECOND rank-IC implementation exists and is DEAD CODE: `def rank_ic(actual, pred) -> float` (pure-Python, tie-aware average ranks). Grep across src/, tests/, scripts/ shows the only importer of validation_stats is significance.py, which imports `excess_kurtosis, mean, sharpe_ratio, stdev` — not rank_ic. Do NOT route the decay curve through it; that would create a second, divergent IC definition.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/validation_stats.py:152 (importer check: src/stock_grader/significance.py:32)`)*
- The ledger record shape is `ResearchRecord` with fields: experiment, market, symbols, targets, horizons: Sequence[int], trials: int, metrics: Mapping[str, float|None], costs: Mapping[str,float], benchmark, leakage_controls, gate_passed: bool, verdict, data_span, code_commit, created_utc, prev_sha256. `horizons` is a purpose-built field that cmd_backtest currently writes as `horizons=[]`. `metrics` values are coerced `None if v is None else float(v)` in `payload()`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/research_manifest.py:63-114 (horizons at :69, payload coercion at :98-100)`)*
- `append_record(path, record)` ALWAYS overwrites the caller's prev_sha256 with the file's actual last line hash: `chained = replace(record, prev_sha256=_last_line_sha256(file_path))`. So records must be appended one at a time in the intended order; you cannot batch-write and you cannot pre-set the chain link.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/research_manifest.py:139-150`)*
- The deflation denominator depends on BOTH the trial COUNT and the trial-Sharpe DISPERSION: `expected_max_sharpe(trial_sharpe_std, n_trials)` returns `0.0` when `n_trials < 2 or trial_sharpe_std <= 0.0`, otherwise `trial_sharpe_std * ((1 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)` with `z1 = norm_ppf(1 - 1/n_trials)`, `z2 = norm_ppf(1 - 1/(n_trials*e))`. `deflated_sharpe_ratio` calls it as `expected_max_sharpe(stdev(usable), n_trials)` where `usable = _finite_trial_sharpes(trial_sharpes)`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/significance.py:149-158, 170-183`)*
- `assess_edge(returns, trial_sharpes, *, alpha=0.05, bootstrap_seed=0, bootstrap_samples=2000, periods_per_year=252) -> SignificanceReport`. `significant = dsr >= (1.0 - alpha) and ci_lo > 0.0`. Verdict is forced to `"INSUFFICIENT SAMPLE -- too few observations to judge an edge."` when `n < 30` — with monthly panels every horizon will hit this for years, and the report must say so rather than hide it.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/significance.py:295-357 (n<30 branch at :325-326)`)*
- cmd_backtest's existing ledger wiring, which cmd_decay must mirror: it reads `prior = load_manifest(ledger_path)`, filters to finite `record["metrics"]["per_period_sharpe"]`, appends this run's `per_period_sharpe(net_spreads)`, then writes ONE record with `trials=len(trial_sharpes)`, `horizons=[]`, `targets=["forward_return"]`, `benchmark="zero"`, non-finite metrics stored as `None`. `--ledger` defaults to `research_ledger.jsonl` and is registered on the p_backtest SUBPARSER.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py:789-857, 1148-1150`)*
- The frozen panel schema (13 columns, verified by reading the parquet): signal_date(str), ticker(str), cik(str), score(float64), letter(str), percentile(float64), coverage(float64), graded(bool), profile(str), config_fingerprint(str), universe_fingerprint(str), code_commit(str), schema_version(str='1.0'). There is NO `filed_through` column and no PIT flag — so a decay panel can never honestly satisfy backtest's `filing_cutoff_provided` contract item.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py:953-970 (writer); frozen_scores/2026-07-30.parquet (82 rows, read back)`)*
- The multi-profile freeze HAS LANDED: `def panel_path(profile): return out_dir / profile / f"{signal_date.isoformat()}.parquet"`, driven by `profiles = profile_names() if getattr(args, "all_profiles", False) else [args.profile]`. `profile_names()` returns 11 names: all_weather, deep_value, dividend_growth, dividend_income, garp, growth, low_volatility, momentum, quality, turnaround, value.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py:904-907; profiles.profile_names()`)*
- A LEGACY flat panel from the pre-profile freeze is still on disk AND git-tracked: `frozen_scores/2026-07-30.parquet` (82 rows, profile column = 'all_weather'). It is the only frozen panel that exists. `git ls-files frozen_scores` returns exactly that one path.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/frozen_scores/2026-07-30.parquet`)*
- Vault EOD day files contain ONLY these fields: close, high, low, open, symbol, transactions, volume, vwap. No adjusted close, no dividend, no delisting proceeds. Example row: `{"close": 53.95, "high": 53.983, "low": 53.45, "open": 53.83, "symbol": "PRFZ", "transactions": 559, "volume": 56218.93163, "vwap": 53.813}`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/2026-07/2026-07-28.jsonl.gz`)*
- The EOD collector requests UNADJUSTED bars: `params={"adjusted": "false", "include_otc": "false", "apiKey": key}`. A split inside a return window therefore shows up as a fake -50%/-75% return.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/market_eod.py:40`)*
- The archive covers roughly two years of sessions (`VaultDataSource.market_eod_available_days()`), one whole-market file per day. `market_eod_series(ticker)` loops over EVERY available day and calls `market_eod_day` inside the loop, so cost is O(tickers x days) whole-file gzip+JSON+DataFrame builds — hours for a modest ticker list. It is unusable for panel building.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/data/vault.py:91-131 (measured against the live vault)`)*
- Vault data is licence-restricted and the adapter is deliberately local-only. Manifest `license_note`: "Massive (ex-Polygon) free-tier data: personal use; read current terms before any redistribution. Private archive." Module docstring: "Access is local-clone-only by design: the repo is private and its license notes prohibit anything that would put raw vendor data behind a URL."  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/2026-07/manifest.json; C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/data/vault.py:1-12`)*
- Stock-Grader is a PUBLIC GitHub repo, Stock-Vault is PRIVATE (`gh repo view` -> {"name":"Stock-Grader","visibility":"PUBLIC"}, {"name":"Stock-Vault","visibility":"PRIVATE"}). Stock-Grader/.gitignore currently lists only: .venv/, __pycache__/, *.py[cod], *.egg-info/, build/, dist/, .pytest_cache/, .coverage, coverage.xml, htmlcov/, .cache/, .DS_Store — no entry for any decay or retro output directory.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/.gitignore`)*
- Total-return reconstruction is not available: the foundry's corporate_actions manifest says `"granularity": "fiscal_period (no ex-dates in XBRL)"` and `"tickers_requested": ["AAPL","JNJ","O"]`; dividends.parquet has 448 rows for 3 tickers with a `dps_current_basis` column and no ex-date; splits.jsonl has 2 rows, both AAPL. `FoundryDataSource.dividends()` docstring states the same limit.  
  *(`C:/Users/tforstrom/Desktop/Stock-Data/data/corporate_actions/manifest.json; C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/data/foundry.py:187-195`)*
- `main()` applies a GLOBAL rule to any subcommand exposing an arg literally named `asof`: `if getattr(args, "asof", None): ... if requested_asof != date.today() and not getattr(args, "pit", False): parser.error("historical --asof requires --pit")`. A decay subcommand with `--asof` would be unrunnable.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py:1214-1220`)*
- `_input_contract` returns exactly five keys — filing_cutoff_provided, point_in_time_universe_attested, total_returns_attested, delistings_included_attested, permanent_identifier_present — and `_attested(panel, column)` requires EVERY row to be non-NA and in {"1","true","yes","y"}. `cmd_backtest` raises unless `args.allow_unverified_panel` when any is False.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:167-196; cli.py:766-775`)*
- The live ledger is already polluted and demonstrates the zero-dispersion trap: research_ledger.jsonl holds 12 records, all with `per_period_sharpe: 2.8284271247461894` and `deflated_sharpe: 0.8970483946339658`. Because stdev of identical trial Sharpes is 0, `expected_max_sharpe` returns 0.0 and the DSR equals the undeflated PSR — 12 recorded 'trials' produced zero deflation. tests/test_cli.py already carries the fix comment: "Point at a scratch ledger: without this the run appends a junk trial to the repo's real research_ledger.jsonl, deflating every future DSR."  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/research_ledger.jsonl; tests/test_cli.py:341-343`)*
- `BacktestConfig.__post_init__` hard-validates: quantiles >= 2, `min_cross_section >= quantiles * 2`, `periods_per_year >= 1`, transaction_cost_bps >= 0, bootstrap_samples >= 0, bootstrap_block_periods >= 1. A computed `periods_per_year` of 0 raises ValueError.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:51-63`)*
- `evaluate_walk_forward` RAISES (not returns) when nothing qualifies: `raise ValueError("no period met the minimum cross-section and score-dispersion requirements")`. Per-signal-date rejection (cross-section < min, or `group["score"].nunique() < 2`) increments `rejected` and continues.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/backtest.py:277-280, 326-329`)*
- The only Stock-Grader GitHub workflow that touches panels is `.github/workflows/monthly-freeze.yml` (cron `19 13 1 * *`, runs `stock-grader freeze --profile all_weather --universe config/universe_default.txt --out frozen_scores` then commits `frozen_scores`). It runs on ubuntu-latest with no access to the private vault clone.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/.github/workflows/monthly-freeze.yml`)*
- Test-fixture builder to reuse: `def build_vault(root: Path, *, corrupt: str | None = None) -> Path` constructs a manifest-complete fake vault under tmp_path with market_eod/2026-07 day files, borrow, and delisted_prices. `_gz_jsonl(rows)` and `_manifest(directory, names, corrupt=...)` are its helpers.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/tests/test_vault.py:16-71`)*
- Project conventions: `[project.scripts] stock-grader = "stock_grader.cli:main"`; `[tool.pytest.ini_options] testpaths=["tests"], pythonpath=["src"]`; `[tool.ruff] line-length = 100`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/pyproject.toml:59-60, 68-71, 85`)*

## Implementation steps

### Step 1. Gitignore the restricted outputs FIRST, before any code that can write them exists

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/.gitignore`

Append to .gitignore:

    # Derived from the PRIVATE Stock-Vault archive (Massive/ex-Polygon, personal-use
    # licence). Stock-Grader is a PUBLIC repo; forward returns must never be pushed.
    signal_decay/
    retro_scores/

Rationale is grounded, not stylistic: `gh repo view` reports Stock-Grader PUBLIC / Stock-Vault PRIVATE, and the vault month manifests carry `license_note: "...personal use; read current terms before any redistribution. Private archive."` This is ECOSYSTEM.md rule 5 ("Decide placement before first write — public git history is forever"). Commit this alone, first.

### Step 2. Add a bulk close-price reader to VaultDataSource (one pass per day file, never per ticker)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/data/vault.py`

Add to `VaultDataSource` in data/vault.py, after `market_eod_series`:

    def market_eod_close_matrix(
        self, symbols: Iterable[str], *,
        start: dt.date | None = None, end: dt.date | None = None,
    ) -> pd.DataFrame:
        """Unadjusted closes for many tickers across many sessions.

        Index: archived session dates, ascending. Columns: the caller's symbols
        verbatim (canonical SEC dash form upstream). Values: float close, NaN
        when the ticker did not trade that session.

        One pass per day file. ``market_eod_series`` re-reads every day file per
        ticker; at 501 sessions x 82 names that is ~2 hours of gzip. Panel work
        must use this.
        """

Implementation contract:
- Build `alias: dict[str, str]` mapping every `ticker_variants(sym)` spelling PLUS the IB space form (`v.replace('-', ' ').replace('.', ' ')`) to the caller's original `sym`, all uppercased. First writer wins; if two requested symbols collide on one alias, raise `VaultError` naming both (do not silently pick one).
- Iterate `self.market_eod_available_days()` filtered by start/end. For each day call `self._read_verified(f"data/market_eod/{day:%Y-%m}", f"{day.isoformat()}.jsonl.gz")` (keeps sha256 verification and the schema_version gate), `gzip.decompress`, split lines, `json.loads` each, and keep only rows whose `str(row["symbol"]).upper()` is in `alias`. Do NOT call `market_eod_day` — building a whole-market DataFrame per day is most of the per-day cost.
- Skip a day whose file is absent from its month manifest (log at WARNING, record the date) rather than raising: a single missing archive day must not kill a 24-month sweep. Return the assembled frame plus expose the skipped days via a new `self.last_skipped_days: list[dt.date]` attribute set on each call.
- Result: `pd.DataFrame(index=pd.DatetimeIndex(days), columns=list(symbols), dtype='float64')`, sorted ascending, with `close` values coerced via `float()`.
Add `from collections.abc import Iterable` to the imports.

### Step 3. Create src/stock_grader/decay.py — module docstring and dataclasses

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

New module. Docstring must state the three structural honesty facts up front: (1) backtest.py forbids two return windows under one signal_date, so a horizon sweep is N physically separate panels, never one wide file; (2) forward returns here are UNADJUSTED PRICE returns from the vault (`adjusted=false`), not total returns — the panels must never write `return_is_total`; (3) each horizon is a separate look and is charged as a separate ledger trial.

Public surface (exact names — do not rename):

    DEFAULT_HORIZONS: tuple[int, ...] = (5, 21, 63, 126)
    PANEL_SCHEMA_VERSION = "1.0"
    ARTIFACT_SCHEMA_VERSION = "1.0"

    @dataclass(frozen=True, slots=True)
    class DecayConfig:
        horizons: tuple[int, ...] = DEFAULT_HORIZONS
        primary_horizon: int = 21
        quantiles: int = 5
        min_cross_section: int = 20
        transaction_cost_bps: float = 10.0
        bootstrap_samples: int = 1_000
        seed: int = 0
        delisting_return: float | None = None      # None = drop and count
        split_screen: bool = True
        non_overlapping_only: bool = False
        min_periods_for_power: int = 36            # REVISED_PLAN section 6 power floor
        min_cross_section_for_power: int = 100
        def __post_init__(self) -> None: ...

    @dataclass(slots=True)
    class HorizonResult:
        horizon_days: int; periods: int; effective_periods: int; overlap_periods: int
        observations: int; dropped_missing_exit: int; dropped_split_suspect: int
        mean_rank_ic: float; rank_ic_interval: tuple[float, float] | None
        rank_ic_information_ratio: float | None; rank_ic_positive_rate: float
        ic_per_sqrt_day: float; mean_net_spread: float
        annualized_spread_sharpe: float | None; is_primary: bool
        unusable_reason: str | None
        descriptive: BacktestReport | None    # all signal dates: unbiased mean IC
        inference: BacktestReport | None      # non-overlapping subsample: honest Sharpe/IR
        significance: SignificanceReport | None

    @dataclass(slots=True)
    class DecayCurve:
        profile: str; archive_through: str; panel_origin: str
        frozen_dir: str; signal_dates: list[str]; config: DecayConfig
        horizons: list[HorizonResult]
        half_life_days: float | None; half_life_fit_r2: float | None; half_life_note: str
        best_horizon_by_ic_ir: int | None; best_horizon_by_ic_per_sqrt_day: int | None
        underpowered: bool; code_commit: str
        config_fingerprint: str; universe_fingerprint: str
        ledger: dict[str, object]; limitations: list[str]
        def to_dict(self) -> dict: ...

`DecayConfig.__post_init__` validates: horizons non-empty, strictly increasing, each in [1, 504], no duplicates; `primary_horizon in horizons`; quantiles >= 2; `min_cross_section >= quantiles * 2` (mirrors BacktestConfig's own guard so the failure surfaces at config time, not mid-sweep); `delisting_return` is None or finite and in [-1.0, 0.0].

### Step 4. decay.py — load_frozen_panels(): read the profile's frozen panels with a fingerprint-drift gate

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

def load_frozen_panels(
        frozen_dir: str | Path, *, allow_fingerprint_drift: bool = False
    ) -> pd.DataFrame:

- `frozen_dir` is the PROFILE directory (e.g. `frozen_scores/all_weather`), never the root. Glob `*.parquet` NON-recursively. This matters: `frozen_scores/2026-07-30.parquet` is a git-tracked legacy flat panel from before the per-profile layout; a recursive glob from the root would silently mix it in. If `frozen_dir` itself contains `*.parquet` AND subdirectories named after `profile_names()`, raise ValueError telling the caller to pass the profile subdirectory.
- Concatenate, then: require the 13 known columns to be present; require `schema_version` to be exactly "1.0" in every row (refuse unknown versions, per ECOSYSTEM rule 1); require a single distinct `profile` value; drop rows where `graded` is False (an ungraded row has no cross-sectional meaning) and count them.
- Fingerprint gate (ECOSYSTEM rule 3, "Two outputs are comparable only when fingerprints match"): if `config_fingerprint` or `universe_fingerprint` has more than one distinct value across signal dates, raise ValueError listing each fingerprint with the dates that carry it — unless `allow_fingerprint_drift=True`, in which case log a WARNING and record the drift so the artifact/markdown carries it as a limitation.
- Return the concatenated frame sorted by (signal_date, ticker), with `signal_date` as `pd.Timestamp`.

### Step 5. decay.py — build_horizon_panel(): one backtest-shaped panel per horizon

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

def build_horizon_panel(
        frozen: pd.DataFrame, closes: pd.DataFrame, horizon: int, *, config: DecayConfig
    ) -> tuple[pd.DataFrame, dict[str, int]]:
        """Returns (panel, counts). counts keys: dropped_missing_anchor,
        dropped_missing_exit, dropped_split_suspect, dropped_incomplete_window."""

Session arithmetic (`sessions = closes.index`, the archived trading days):
- `entry_i` = index of the FIRST session strictly greater than signal_date. If none, the whole signal date is unusable (`dropped_incomplete_window`). Strictly-greater is required by backtest.py:141 and also avoids executing at a close whose data fed the score.
- `exit_i = entry_i + horizon`. If `exit_i >= len(sessions)`, the horizon has not completed for that signal date — drop the whole signal date and count it. This is what makes a long horizon unavailable until the archive catches up; it must be a clean drop, never a truncated window.
- `return_start = sessions[entry_i].date()`, `return_end = sessions[exit_i].date()`. Both are identical for every ticker in the panel — that is exactly what satisfies backtest.py:283-286.
- `forward_return = close[exit_i] / close[entry_i] - 1.0` per ticker.

Per-observation drops (each counted, never silent):
- entry close NaN -> `dropped_missing_anchor`.
- exit close NaN (the name stopped trading inside the window) -> if `config.delisting_return is None`: drop, count `dropped_missing_exit`. Else set `forward_return = config.delisting_return` and still count it. Dropping is the survivorship leak; the count is the honesty control that makes it visible.
- Split screen (`config.split_screen`): for each ticker walk adjacent archived sessions in `[entry_i, exit_i]` and compute `r = c[t] / c[t-1]`. Mark `split_suspect` if for any k in `(2,3,4,5,6,7,8,10,15,20)` either `abs(r - k) <= 0.02 * k` or `abs(r - 1.0/k) <= 0.02 / k`. Drop and count `dropped_split_suspect`. Reason: the collector requests `adjusted=false` (Stock-Vault market_eod.py:40) and the foundry's splits.jsonl covers 2 rows for 1 ticker, so there is no adjustment table to join. This heuristic also removes genuine 1-day moves of that magnitude — say so in the limitations, and report the count so a human can judge.

Emitted panel columns (exactly these; do NOT add attestation columns the data cannot back):
  signal_date, return_start, return_end, ticker, cik, score, forward_return,
  horizon_days, entry_close, exit_close, profile, config_fingerprint,
  universe_fingerprint, code_commit, panel_schema_version
Omit `filed_through`, `universe_is_pit`, `return_is_total`, `delisting_return_included` entirely — the frozen panel carries no filing cutoff, the universe is today's survivors, and the closes are price-only. `_permanent_id_column` will find `cik`, so `permanent_identifier_present` is the one contract item that legitimately passes.
Finally assert locally that `panel.groupby('signal_date')[['return_start','return_end']].nunique().max().max() == 1` before returning — fail loudly in the builder rather than deep inside `evaluate_walk_forward`.

### Step 6. decay.py — evaluate_decay(): the dual-view evaluation that keeps overlap honest

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

def evaluate_decay(
        frozen_dir, vault_root, *, profile: str, config: DecayConfig,
        ledger_path: Path, allow_fingerprint_drift: bool = False,
    ) -> tuple[DecayCurve, dict[int, pd.DataFrame]]:

Sequence:
1. `frozen = load_frozen_panels(...)`; `vault = VaultDataSource(vault_root)`; `closes = vault.market_eod_close_matrix(sorted(frozen['ticker'].unique()), start=min_signal_date, end=None)`. `archive_through = closes.index.max().date().isoformat()`.
2. Measure the panel cadence empirically — do not assume monthly: `spacing = median number of archived sessions between consecutive signal dates`. Then per horizon `overlap_periods = max(1, ceil(horizon / spacing))`.
3. For each horizon build the panel, then run `evaluate_walk_forward` TWICE:
   - descriptive view: ALL signal dates, `BacktestConfig(quantiles, min_cross_section, periods_per_year=round(252/spacing) clamped to >=1, transaction_cost_bps, bootstrap_samples, bootstrap_block_periods=max(3, overlap_periods), seed)`. Its `mean_rank_ic` and `rank_ic_positive_rate` are the honest headline: the MEAN of a per-date cross-sectional statistic is unbiased under window overlap; only its standard error is understated.
   - inference view: signal dates subsampled at stride `overlap_periods` starting at offset 0 (fixed offset, declared in advance — never search offsets), `periods_per_year = max(1, round((252/spacing) / overlap_periods))`, `bootstrap_block_periods=3`. Its `net_spread` series is the ONLY series fed to `assess_edge`, and its IR/intervals are the ones quoted as inferential. When `overlap_periods == 1` the two views are the same panel; still run both so the code path is uniform.
   - `config.non_overlapping_only=True` skips the descriptive view and uses the subsample everywhere.
4. Wrap each `evaluate_walk_forward` call in `try/except ValueError` — it RAISES "no period met the minimum cross-section and score-dispersion requirements" when a horizon has no complete window yet. Record `HorizonResult(unusable_reason=str(exc), descriptive=None, inference=None, significance=None)` and CONTINUE; one immature horizon must not abort the sweep.
5. `ic_per_sqrt_day = mean_rank_ic / sqrt(horizon_days)`.
6. `underpowered = any(view.periods < config.min_periods_for_power) or (median cross-section < config.min_cross_section_for_power)`.
7. If EVERY horizon is unusable, raise ValueError: "no horizon has a completed forward window: the archive ends {archive_through} and the newest signal date is {d}; the shortest horizon needs {n} more archived sessions." (This is the state today — one frozen panel dated 2026-07-30 against an archive ending 2026-07-28.)

### Step 7. decay.py — fit_half_life(): turn the four points into a decay statement (and refuse when it can't)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

def fit_half_life(results: Sequence[HorizonResult]) -> tuple[float | None, float | None, str]:
        """Returns (half_life_days, r2, note)."""

OLS of `ln(mean_rank_ic)` on `horizon_days` over usable horizons with `mean_rank_ic > 0`. `lambda_ = -slope`; `half_life = ln(2) / lambda_`.
Refusal rules, each with a distinct note string:
- fewer than 3 horizons with positive mean IC -> (None, None, "too few positive-IC horizons to fit a decay curve").
- `lambda_ <= 0` (IC flat or RISING with horizon — the likely real outcome for a fundamentals score) -> (None, r2, "IC does not decay across the horizons tested; the natural holding period is at or beyond {max_h}d — extend the sweep before concluding").
- `r2 < 0.5` -> (None, r2, "exponential decay fits poorly (R2={r2:.2f}); the four points do not describe a clean decay").
Never report a half-life the fit does not support. Also compute `best_horizon_by_ic_ir` (argmax of the inference view's `rank_ic_information_ratio`, None if all None) and `best_horizon_by_ic_per_sqrt_day` (argmax of `ic_per_sqrt_day`), and note in the markdown that these are post-hoc argmaxes over the same sweep, not selections that clear a gate.

### Step 8. decay.py — record_sweep_trials(): every horizon is its own ledger trial, on ONE shared denominator

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

def record_sweep_trials(
        curve: DecayCurve, *, ledger_path: Path, alpha: float = 0.05
    ) -> dict[str, object]:

This is the honesty core of M2. Design decisions, each deliberate:

(a) ONE ResearchRecord PER HORIZON. `experiment=f"signal_decay:{profile}:h{h}"`, `horizons=[h]` — populating the `horizons: Sequence[int]` field that research_manifest.py:69 defines and cmd_backtest currently leaves as `[]`. Four horizons on one profile append four lines; `--all-profiles` appends 4 x 11 = 44.

(b) ONE SHARED DENOMINATOR, ORDER-INDEPENDENT. Compute BEFORE appending anything:

    prior = [m for r in load_manifest(ledger_path)
             if isinstance(r.get("metrics"), dict)
             and isinstance((m := r["metrics"].get("per_period_sharpe")), (int, float))
             and math.isfinite(m)]
    sweep = [per_period_sharpe(h.inference net_spreads) for each usable horizon, finite only]
    trial_sharpes = prior + sweep        # the SAME list for every horizon record
    n_denominator = len(trial_sharpes)

Then `assess_edge(net_spreads_h, trial_sharpes, periods_per_year=pp_y_h, bootstrap_seed=config.seed)` per horizon, and every record carries `trials=n_denominator`. If instead you appended horizon-by-horizon and re-read the ledger between appends (the naive mirror of cmd_backtest), the last horizon evaluated would face a bigger N than the first — the reported DSR would depend on iteration order. Every horizon in one sweep is charged for every look in that sweep, equally.

(c) THE DENOMINATOR EFFECT, STATED. `expected_max_sharpe(stdev(trial_sharpes), n_trials)` grows roughly as sigma_trials * sqrt(2 ln N). Going from the current N=12 to N=16 (one 4-horizon sweep) raises the bar by ~sqrt(ln16/ln12) ~= 1.12x; a `--all-profiles` sweep to N=56 raises it ~1.27x. Write this into the record's `leakage_controls` string verbatim so the ledger itself explains its own arithmetic:

    leakage_controls=(
      f"horizon sweep {list(config.horizons)}d, primary={config.primary_horizon}d; "
      f"one trial per horizon on a shared denominator of {n_denominator}; "
      "inference on non-overlapping subsample (stride "
      f"{overlap}, fixed offset 0); "
      "E[max] deflation assumes INDEPENDENT trials — horizons of one score are "
      "highly correlated, so this correction OVER-deflates; the remedy is a "
      "pre-declared primary horizon, not a private discount; "
      f"panel contract: FAILED {','.join(failed_contract)}"
    )

(d) PRE-DECLARED PRIMARY. `gate_passed = bool(is_primary and sig and sig.significant)`. Non-primary horizons get `gate_passed=False` unconditionally and `verdict = "EXPLORATORY (non-primary horizon) -- " + sig.verdict`; the primary gets `"PRIMARY (pre-declared) -- " + sig.verdict`. A sweep must never be able to promote its own argmax to a passed gate. This is the cheap slice of the deferred "pre-registration record kind" (HANDOFF section 10).

(e) `metrics` = {per_period_sharpe, mean_net_spread, mean_rank_ic, deflated_sharpe, rank_ic_information_ratio, ic_per_sqrt_day, effective_periods (as float)} — every non-finite value stored as `None`, never NaN (research_manifest.py coerces `None if v is None else float(v)`; a NaN in the ledger poisons stdev and therefore every future DSR — the exact bug tests/test_cli.py:405 guards).
`costs` = {"transaction_cost_bps": float(...), "delisting_return": float(config.delisting_return) if set else 0.0}. `benchmark="zero"`, `market="us_equities"`, `targets=["forward_return"]`, `symbols=[]`, `data_span=f"{first_signal_date}..{last_signal_date}"`, `code_commit=current_commit()`.

(f) Append with `append_record(ledger_path, rec)` one at a time in ascending horizon order — it rewrites `prev_sha256` from the file's last line, so the chain is only correct if you append sequentially. Return `{"path": str(ledger_path), "prior_trials": len(prior), "trials_added": len(sweep), "lifetime_trials": n_denominator, "deflated_benchmark_sr": ..., "record_hashes": {h: written.integrity_sha256() ...}}` where `written = append_record(...)` — the CHAINED record the function returns. `rec.integrity_sha256()` computed on the pre-append object is never the hash on the written line (chaining sets `prev_sha256` inside the hashed payload), and this dict is baked into an immutable dated `decay.json` that must reconcile with the ledger forever.

(g) Emit NO summary/best-horizon record. That would double-charge the same data and enshrine a selection.

### Step 9. decay.py — write_decay_artifacts(): manifest-carrying output directory

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

def write_decay_artifacts(
        curve: DecayCurve, panels: dict[int, pd.DataFrame], out_root: str | Path
    ) -> Path:

Layout (one directory per profile per archive cut — regenerable, not append-only):

    signal_decay/<profile>/<archive_through>/
        manifest.json
        panel_h005.parquet          # zero-padded to 3 so lexical order == numeric
        panel_h021.parquet
        panel_h063.parquet
        panel_h126.parquet
        decay.json
        decay.md

Write each file via `tmp = path.with_suffix(path.suffix + '.tmp'); ...; tmp.replace(path)` (the atomic pattern cmd_freeze already uses at cli.py:974-976).

`manifest.json` mirrors the foundry/vault contract that FoundryDataSource.manifest and VaultDataSource._manifest enforce, so any future consumer can verify it the same way:
    schema_version: "1.0"
    generated_at_utc, code_commit, profile, archive_through, panel_origin
    primary_horizon, horizons
    config_fingerprint, universe_fingerprint
    files: [{name, sha256, bytes, rows}]  # rows omitted for decay.md
    sources: {frozen_panels: [{path, sha256}], vault_eod_months: [{dataset, manifest_sha256}]}
    license_note: "PRIVATE — forward returns derived from the Stock-Vault archive (Massive/ex-Polygon, personal-use licence). Do not publish or commit to a public repository."

`decay.json` is a NEW document and defines its own top level — but embed `BacktestReport.to_dict()` and the SignificanceReport verbatim under `horizons[i].descriptive_report` / `.inference_report` / `.significance`. Do not restructure or re-key them (HANDOFF working rule 2: never wrap a documented schema in an envelope; add keys additively).

After writing, best-effort check that the output is ignored: run `git check-ignore -q <out_root>` (same defensive `subprocess.run(..., check=False)` style as `current_commit()`); on a non-zero return print a loud red warning that the directory is NOT gitignored and contains restricted vendor-derived data. Never raise on this — never let a git probe break the run.

### Step 10. decay.py — decay_to_markdown(): the report a human actually reads

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`

def decay_to_markdown(curve: DecayCurve) -> str:

Sections, in order:
1. Title `# Signal decay — <profile>` plus the not-investment-advice line (AGENTS.md: "All output shown to users must carry the not-investment-advice framing").
2. Provenance header (this also closes HANDOFF section 9's "Provenance header" item for a third renderer): `asof/archive_through`, `panel_origin`, `code_commit`, `config_fingerprint[:12]`, `universe_fingerprint[:12]`, number of signal dates, first..last.
3. A one-paragraph WHAT THIS IS NOT block: surviving-universe, unadjusted price returns (no dividends, no delisting proceeds), no filing-cutoff proof — i.e. 4 of backtest's 5 contract items fail by construction and the run requires `--allow-unverified-panel`.
4. The decay table, one row per horizon:
   `| horizon | periods | eff. periods | overlap | obs | mean rank IC | IC 95% MB interval | IC IR | IC>0 rate | IC/sqrt(d) | mean net spread | ann. spread Sharpe | DSR | dropped (exit/split) | trial |`
   The `trial` column reads `PRIMARY` or `exploratory`. Percent-format the spread columns, 3-decimal the IC columns (mirror `backtest_to_markdown`'s `number()`/`interval()` helpers rather than inventing new formatting).
5. An ASCII bar of `mean_rank_ic` vs horizon so the SHAPE is visible in a terminal — one line per horizon, bar length proportional to |IC| against the max |IC| in the sweep, e.g. `  21d | ############------ | +0.041`. Negative ICs draw to the left of a zero marker.
6. `## Reading the curve` — the half-life line (or the explicit refusal note from `fit_half_life`), `best_horizon_by_ic_ir`, `best_horizon_by_ic_per_sqrt_day`, and one plain sentence: "Under a fixed rebalancing budget the argmax of IC/sqrt(days) is the holding-period estimate; both argmaxes are post-hoc over this same sweep and neither clears a gate."
7. `## Multiple-testing charge` — prior trials, trials added, lifetime trials, the deflated benchmark Sharpe used, and the correlated-looks over-deflation caveat in full sentences. If `stdev(trial_sharpes) == 0` say so explicitly: "every recorded trial has the same Sharpe, so the E[max] benchmark is 0 and NOTHING was deflated — do not read this as having survived the correction." (The live ledger is in exactly that state: 12 records, all Sharpe 2.8284271247461894.)
8. `## Power` — the `underpowered` flag with the floors (>=36 periods, >=100 names) and, when any `SignificanceReport.n_obs < 30`, the fact that `assess_edge` forces "INSUFFICIENT SAMPLE" regardless of the numbers above.
9. `## Limitations` — the accumulated `curve.limitations` list, including split-screen drop counts, missing-exit drop counts, fingerprint drift if overridden, and any archive days the vault reader skipped.

### Step 11. cli.py — add cmd_decay and its SUBPARSER (every arg on the subparser)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/__init__.py`

Add `def cmd_decay(args: argparse.Namespace) -> int:` next to `cmd_backtest`, and register:

    p_decay = sub.add_parser(
        "decay",
        help="measure the score's rank-IC decay across holding horizons (5/21/63/126d)",
    )
    p_decay.add_argument("--frozen-dir", default="frozen_scores",
                         help="root of the frozen panels; the profile subdirectory is appended")
    p_decay.add_argument("--vault", required=True,
                         help="local Stock-Vault clone (private, local-only by design)")
    p_decay.add_argument("--profile", default="all_weather", choices=profile_names())
    p_decay.add_argument("--all-profiles", action="store_true",
                         help="sweep every profile; charges len(horizons) x 11 trials to the ledger")
    p_decay.add_argument("--horizons", nargs="+", type=_positive_int, default=[5, 21, 63, 126])
    p_decay.add_argument("--primary-horizon", type=_positive_int, default=21,
                         help="the ONE pre-declared horizon allowed to pass the gate; "
                              "every other horizon is recorded as exploratory")
    p_decay.add_argument("--out", default="signal_decay")
    p_decay.add_argument("--quantiles", type=_positive_int, default=5)
    p_decay.add_argument("--min-cross-section", type=_positive_int, default=20)
    p_decay.add_argument("--transaction-cost-bps", type=float, default=10.0)
    p_decay.add_argument("--bootstrap-samples", type=int, default=1_000)
    p_decay.add_argument("--seed", type=int, default=0)
    p_decay.add_argument("--delisting-return", type=float, default=None,
                         help="Shumway-style imputation for names that stop trading mid-window "
                              "(e.g. -0.30); default drops and counts them")
    p_decay.add_argument("--no-split-screen", dest="split_screen",
                         action="store_false", default=True)
    p_decay.add_argument("--non-overlapping", action="store_true")
    p_decay.add_argument("--archive-through", default=None,
                         help="ignore vault sessions after this ISO date")
    p_decay.add_argument("--allow-fingerprint-drift", action="store_true")
    p_decay.add_argument("--allow-unverified-panel", action="store_true")
    p_decay.add_argument("--ledger", default="research_ledger.jsonl",
                         help="append-only trial ledger shared with `backtest`; the "
                              "deflated-Sharpe correction counts every trial ever recorded")
    p_decay.add_argument("--format", default="text", choices=["text", "json", "md"])
    p_decay.set_defaults(func=cmd_decay)

HARD CONSTRAINT: the date arg is `--archive-through`, NOT `--asof`. `main()` at cli.py:1214 applies `if getattr(args, "asof", None): ... parser.error("historical --asof requires --pit")` to EVERY subcommand; naming it `asof` makes `decay` unrunnable for any historical date. Equally, every arg lives on `p_decay` (not the top-level parser) — HANDOFF working rule 3, the "DOA workflow" bug class that "silently killed every scheduled run in two repos".

Add a validation block in `main()` beside the existing `command == "backtest"` block:
  - `--quantiles >= 2`, `--min-cross-section >= 2 * quantiles`, `--transaction-cost-bps` finite and >= 0, `--bootstrap-samples >= 0`;
  - horizons strictly increasing, unique, each <= 504;
  - `--primary-horizon` must be in `--horizons` (error text: "--primary-horizon must be one of --horizons: declare which horizon you are testing BEFORE you look at the others");
  - `--delisting-return`, when given, finite and in [-1, 0].

`cmd_decay` body: resolve `frozen_dir = Path(args.frozen_dir) / profile` per profile; loop profiles (`profile_names()` when `--all-profiles`, printing a loud warning of the trial cost first); for each, `evaluate_decay` -> `record_sweep_trials` -> `write_decay_artifacts`; enforce the same contract refusal cmd_backtest uses (any failed `input_contract` item requires `--allow-unverified-panel`, with a message that names the failing items and states that on this data 4 of 5 always fail); print the ledger line to `status_console` (stderr) exactly as cmd_backtest does at cli.py:854-857 so `--format json` stays parseable on stdout; `--format json` prints `curve.to_dict()`, `md` prints `decay_to_markdown`, `text` renders it through `console.print(Markdown(...))`. Return 0, or 2 when no horizon is usable.

Export `DecayConfig, DecayCurve, HorizonResult, decay_to_markdown, evaluate_decay` from `stock_grader/__init__.py` alongside the backtest exports, keeping `__all__` alphabetically sorted (it currently is).

### Step 12. tests/test_decay.py — new suite (every test points --ledger at tmp_path)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/tests/test_decay.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/tests/conftest.py`

Reuse `build_vault`, `_gz_jsonl`, `_manifest` from tests/test_vault.py by importing them (`from .test_vault import build_vault` will not work without a package; instead extend the fixture helpers into a shared `tests/conftest.py` fixture `fake_vault(tmp_path)` OR import via `from test_vault import build_vault` which works under pytest's `rootdir`-on-syspath — verify which the existing suite already relies on before choosing).

Build a richer synthetic vault: >= 30 monthly signal dates' worth of sessions, >= 60 tickers, and construct closes so forward IC decays with a KNOWN exponential half-life. Write these tests, exact names:

1. `test_close_matrix_reads_each_day_once_and_bridges_ticker_spellings` — asks for `BRK-B` (SEC dash), finds the archive's `BRK.B`; asserts columns equal the requested symbols; asserts a monkeypatched counter shows each day file opened exactly once.
2. `test_horizon_panel_passes_backtest_validation_and_window_chronology` — `evaluate_walk_forward` accepts each emitted panel; `return_start > signal_date` and `return_end > return_start` for every row.
3. `test_one_signal_date_maps_to_exactly_one_return_window_per_panel` — positive: each panel evaluates cleanly. Negative control: `pd.concat([panel_h5, panel_h21])` raises `ValueError` matching `"mixes return windows"` OR `"duplicate"` — proving the one-file-per-horizon layout is forced by backtest.py, not chosen.
4. `test_missing_exit_price_is_dropped_and_counted_not_silently_survivorship_filtered` — a ticker whose closes stop mid-window: `counts["dropped_missing_exit"] == 1` and the ticker is absent from the panel; with `delisting_return=-0.30` it is present with that value and still counted.
5. `test_split_suspect_screen_drops_unadjusted_split_jumps` — plant a clean 4:1 price halving-to-quarter step; assert the observation is dropped and `dropped_split_suspect == 1`; assert `split_screen=False` keeps it.
6. `test_each_horizon_is_a_separate_ledger_trial_with_one_shared_denominator` — 3 horizons on an EMPTY tmp ledger: exactly 3 records appended; `[r["horizons"] for r in records] == [[5],[21],[63]]`; every `r["trials"] == 3` (order-independence); `verify_chain(records)` and `all(verify_line(r) for r in records)` true. Then re-run: 6 records, the last three all `trials == 6`.
7. `test_non_primary_horizons_are_marked_exploratory_and_cannot_pass_the_gate` — even when a non-primary horizon's `significance.significant` is True, its record has `gate_passed is False` and its verdict starts with `"EXPLORATORY"`; only the `--primary-horizon` record can carry `gate_passed True`.
8. `test_overlapping_windows_use_a_non_overlapping_subsample_for_inference` — with monthly spacing and h=63, `overlap_periods == 3`, `inference.periods == ceil(descriptive.periods / 3)`, and the Sharpe fed to `assess_edge` came from the subsample.
9. `test_planted_decay_curve_is_recovered_within_tolerance` — calibration: with a planted half-life of ~30 trading days, `fit_half_life` returns a value within +/-40% and `r2 >= 0.8`.
10. `test_half_life_refuses_when_ic_does_not_decay` — flat/rising IC yields `half_life_days is None` and a note containing `"does not decay"`.
11. `test_decay_refuses_when_no_horizon_has_a_completed_forward_window` — one signal date at the end of the archive: `ValueError` whose message names the archive end date and the shortfall in sessions.
12. `test_decay_refuses_mixed_fingerprints_without_override` — two frozen dates with different `config_fingerprint`: `load_frozen_panels` raises; `allow_fingerprint_drift=True` succeeds and the limitation string appears in the markdown.
13. `test_decay_ignores_the_legacy_flat_frozen_panel` — a stray `frozen_scores/2026-07-30.parquet` beside `frozen_scores/all_weather/` is never loaded when `--frozen-dir frozen_scores --profile all_weather`.
14. `test_decay_cli_args_are_registered_on_the_subparser` — `build_parser().parse_args(["decay", "--vault", "v", "--horizons", "5", "21", "--primary-horizon", "21", "--ledger", "l.jsonl"])` parses; `parse_args(["decay", "--vault", "v", "--primary-horizon", "7"])` SystemExits (7 not in horizons). Guards the DOA-workflow class.
15. `test_decay_artifact_manifest_hashes_every_emitted_file` — every file in the output directory (except manifest.json itself) is listed with a matching sha256; `manifest["schema_version"] == "1.0"`; `license_note` contains `"PRIVATE"`.
16. `test_decay_markdown_reports_curve_half_life_power_and_trial_charge` — output contains the provenance header, the per-horizon table, an ASCII bar row, the primary-horizon declaration, the correlated-looks over-deflation sentence, and the not-investment-advice line.
17. `test_zero_dispersion_trial_set_reports_that_nothing_was_deflated` — seed the ledger with identical Sharpes; assert the markdown contains "NOTHING was deflated" (the live-ledger state).

Every test that invokes cmd_decay MUST pass `ledger=str(tmp_path / "ledger.jsonl")` — the repo ledger already carries 12 junk records from an earlier leak, and tests/test_cli.py:341-343 documents exactly this trap.

### Step 13. docs/SIGNAL-DECAY.md + a LOCAL monthly workflow (deliberately not a GitHub Action)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/docs/SIGNAL-DECAY.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/scripts/monthly_decay.ps1`, `C:/Users/tforstrom/Desktop/Stock-Grader/README.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/REVIEW_FEEDBACK.md`

Write `docs/SIGNAL-DECAY.md` covering: why one panel per horizon (quote backtest.py:281-286); the entry/exit session convention; the overlap/dual-view rule; the ledger charging model and the over-deflation caveat; the artifact layout; how to read the curve; and the data limits (price-only returns, surviving universe, unadjusted closes).

Workflow — do NOT add a `.github/workflows/signal-decay.yml`. The vault is PRIVATE and `VaultDataSource`'s docstring states access is "local-clone-only by design"; the monthly-freeze Action runs on ubuntu-latest with no vault, and the outputs are licence-restricted in a PUBLIC repo. Instead add `scripts/monthly_decay.ps1`, run locally (optionally via Windows Task Scheduler, the same tolerance HANDOFF item 7 grants the vault mirror because it is a local/backup job, not the primary clock):

    stock-grader decay --vault C:/Users/tforstrom/Desktop/Stock-Vault `
      --frozen-dir C:/Users/tforstrom/Desktop/Stock-Grader/frozen_scores `
      --profile all_weather --primary-horizon 21 `
      --allow-unverified-panel --format md

PowerShell 5.1 constraints (HANDOFF rule 4): no `&&`/`||` chaining — use `; if ($?) { ... }`; never `2>$null` a native command; use backtick line continuation as above.
The script must self-check staleness before running (HANDOFF item 7's pattern): fail with a clear message if the newest vault EOD session is older than 4 days, or if the newest frozen panel is older than 40 days — a decay curve computed on a stalled archive is worse than none.

Add a `docs/SIGNAL-DECAY.md` row to README's docs list, and log the completed item in `docs/REVIEW_FEEDBACK.md`'s Agent log with the commit hash (AGENTS.md reviewer loop).

### Step 14. LAST, and only after the suite is green: add `filed_through` to the freeze panel, additively

*Files:* `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/decay.py`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/SIGNAL-DECAY.md`

Do this last because cli.py is being edited concurrently. In `cmd_freeze`'s row dict (currently 13 keys at cli.py:953-970) add ONE key:

    "filed_through": signal_date.isoformat() if getattr(args, "pit", False) else None,

Keep `schema_version` at "1.0" — this is an additive key, and readers that never look for it are unaffected (HANDOFF working rule 2; the same 'additive, bump nothing' call HANDOFF item 5 makes for events.jsonl). Do NOT set it unconditionally: without `--pit` the grader used whatever was in the SEC cache at freeze time, and writing `filed_through = signal_date` would be an unearned attestation that flips backtest's `filing_cutoff_provided` to true on data that cannot support it.

Make `build_horizon_panel` propagate `filed_through` into the decay panel ONLY when the frozen panel carries the column with a non-null value for every row. Existing panels lack it, so nothing changes today; a future `freeze --pit` run earns the contract item automatically.

Also document (docs/SIGNAL-DECAY.md, not code) the retro-panel escape hatch and its distinct root: `stock-grader freeze --asof 2024-09-30 --pit --all-profiles --out retro_scores` can manufacture history back to the archive start (2024-07-29), and `decay --frozen-dir retro_scores` will consume it — but retro panels are survivorship-biased (today's universe) and their PIT vintage is compromised by the SEC cache's 24h TTL (REVISED_PLAN section 6). They must NEVER be written into `frozen_scores/`, and `DecayCurve.panel_origin` must record `retro_backfilled` vs `forward_frozen` (derive it from the `--frozen-dir` basename, and put it in the manifest and the markdown provenance header).

## Pitfalls

- NAME COLLISION THAT MAKES THE COMMAND UNRUNNABLE: `main()` at cli.py:1214 applies `if getattr(args, "asof", None): ... parser.error("historical --asof requires --pit")` to EVERY subcommand. A `decay --asof` arg would error out for any historical date. Use `--archive-through`.
- DOA-WORKFLOW BUG CLASS (HANDOFF working rule 3): every decay arg must be registered on the `p_decay` subparser, never on the top-level parser. This 'silently killed every scheduled run in two repos'. Test 14 in the plan exists solely to guard it.
- LICENSING LANDMINE: Stock-Grader is PUBLIC (verified via `gh repo view`), Stock-Vault is PRIVATE, and the vault month manifests say 'personal use; read current terms before any redistribution. Private archive.' Decay panels contain `forward_return`, `entry_close`, `exit_close` derived from that archive. `.gitignore` must gain `signal_decay/` and `retro_scores/` BEFORE the writer exists. This is ECOSYSTEM.md rule 5, and public git history is forever.
- DO NOT BUILD A GITHUB ACTION FOR THIS. `VaultDataSource`'s docstring: 'Access is local-clone-only by design: the repo is private and its license notes prohibit anything that would put raw vendor data behind a URL.' The existing monthly-freeze.yml runs on ubuntu-latest with no vault. Decay is a LOCAL job.
- PERFORMANCE TRAP: `VaultDataSource.market_eod_series` calls `market_eod_day` inside a loop over all 501 archived days — per ticker. At 0.19 s/day and 82 tickers that is ~2 hours per panel build. Never call it here; add and use `market_eod_close_matrix`.
- UNADJUSTED CLOSES: the collector requests `adjusted=false` (Stock-Vault/src/stock_vault/market_eod.py:40) and the foundry's splits.jsonl has exactly 2 rows for 1 ticker — there is no adjustment table to join at universe scale. A 4:1 split inside a window reads as -75%. The heuristic split screen is mandatory, and its drop count must be reported because it also removes genuine crashes.
- NO TOTAL RETURNS EXIST: foundry dividends are `granularity: fiscal_period (no ex-dates in XBRL)` for 3 tickers. Never write a `return_is_total` column, and never write `universe_is_pit` or `delisting_return_included`. `_attested` only needs one truthy value per row to flip the contract to 'yes' — writing them would be a lie the evaluator would then repeat in its own limitations section. 4 of 5 contract items fail by construction; `--allow-unverified-panel` is the correct, permanent state of this measurement.
- SURVIVORSHIP THROUGH SILENT NaN: a ticker that stops trading has no exit close, so a naive `dropna()` deletes exactly the losers. Count `dropped_missing_exit` per horizon and print it; `--delisting-return` imputation is opt-in and does NOT earn the delistings attestation.
- LEDGER ORDER-DEPENDENCE: mirroring cmd_backtest naively (read ledger, append, read ledger, append...) makes the last horizon face a bigger trial count than the first, so the reported DSR depends on iteration order. Compute prior-trials ONCE, add all of this sweep's horizon Sharpes, and give every record the same `trials` value; then append sequentially (append_record rewrites prev_sha256 from the file, so batching is impossible and ordering matters for the chain).
- ZERO-DISPERSION DEFLATION ILLUSION: `expected_max_sharpe` returns 0.0 when `trial_sharpe_std <= 0.0`, so identical trial Sharpes produce NO deflation at all. The live research_ledger.jsonl is in exactly that state — 12 records, all Sharpe 2.8284271247461894, DSR 0.897 = the undeflated PSR. The markdown must call this out rather than let a reader think 12 trials were corrected for.
- OVER-DEFLATION IS ALSO A LIE IF UNSTATED: `expected_max_sharpe` assumes independent trials. Four horizons of one score are highly correlated, so the correction is conservative. State it in `leakage_controls` and the markdown; do NOT silently apply a private discount factor — the remedy is the pre-declared `--primary-horizon`.
- NaN IN THE LEDGER IS CONTAGIOUS: one NaN `per_period_sharpe` makes `stdev(trial_sharpes)` NaN and therefore every future DSR NaN. Store non-finite metrics as `None`. tests/test_cli.py:405 (`test_backtest_null_sharpe_trial_does_not_poison_later_deflation`) is the regression guard for this exact bug.
- TESTS MUST NOT WRITE TO THE REPO LEDGER: `--ledger` defaults to `research_ledger.jsonl` in the repo root. Twelve junk records are already there from an earlier leak (research_ledger.jsonl is currently modified in git status). Every test passes `ledger=str(tmp_path / 'ledger.jsonl')`; tests/test_cli.py:341-343 carries the comment explaining why.
- LEGACY FLAT PANEL: `frozen_scores/2026-07-30.parquet` is git-tracked and sits OUTSIDE the new `frozen_scores/<profile>/` layout. A recursive glob from the frozen root would silently mix it into a per-profile sweep. Glob the profile directory non-recursively and refuse ambiguity.
- OVERLAPPING WINDOWS WITH MONTHLY PANELS: at h=63 with monthly signal dates each window overlaps the next two. Mean rank IC stays unbiased (it is a mean of per-date cross-sectional statistics), but the IC information ratio, the moving-block interval, the spread Sharpe, PSR and DSR are all inflated. Feed `assess_edge` only the non-overlapping subsample, and label the full-panel view descriptive.
- BacktestConfig VALIDATES IN __post_init__: a computed `periods_per_year` of 0 raises ValueError ('periods_per_year must be positive'), and `min_cross_section >= quantiles * 2` is enforced. Clamp the derived `periods_per_year` to >= 1 before constructing.
- evaluate_walk_forward RAISES rather than returning empty when no period qualifies ('no period met the minimum cross-section and score-dispersion requirements'). At today's data every horizon raises. Catch per horizon, record `unusable_reason`, and only fail the whole run when ALL horizons are unusable.
- assess_edge FORCES 'INSUFFICIENT SAMPLE -- too few observations to judge an edge.' whenever n < 30. With monthly panels, 126d non-overlapping inference has n = periods/6 — it will read INSUFFICIENT for many years. The report must present that honestly rather than burying it under a DSR number.
- IT IS NOT RUNNABLE ON TODAY'S DATA. One frozen panel exists (2026-07-30) and the vault archive ends 2026-07-28 — zero completed forward windows for any horizon. Build against a synthetic vault in tests, verify the refusal path is the one that fires on real data, and do not report placeholder numbers.
- SUITE HYGIENE (HANDOFF working rules 1 and 6): run `pip install -e ".[dev]"` first — two subprocess tests import the live tree. Full suite is ~20 min; use targeted tests per chunk and a background full run per 2-3 chunks. One coherent chunk per commit, green before AND after. ruff line-length is 100.
- DO NOT resurrect `validation_stats.rank_ic()` (dead code, no importers) as a second IC path. The one production definition is pandas Spearman at backtest.py:288; two definitions that disagree on tie handling would make the decay curve incomparable to every backtest already in the ledger.

## Acceptance criteria

Do not report this milestone complete until every box is checkable.

- [ ] `cd C:/Users/tforstrom/Desktop/Stock-Grader && pip install -e ".[dev]" && pytest -q` is green (expect ~570 + the ~17 new decay tests, 0 failed), both before the first commit and after the last.
- [ ] `pytest -q tests/test_decay.py` passes all of: test_close_matrix_reads_each_day_once_and_bridges_ticker_spellings, test_horizon_panel_passes_backtest_validation_and_window_chronology, test_one_signal_date_maps_to_exactly_one_return_window_per_panel, test_missing_exit_price_is_dropped_and_counted_not_silently_survivorship_filtered, test_split_suspect_screen_drops_unadjusted_split_jumps, test_each_horizon_is_a_separate_ledger_trial_with_one_shared_denominator, test_non_primary_horizons_are_marked_exploratory_and_cannot_pass_the_gate, test_overlapping_windows_use_a_non_overlapping_subsample_for_inference, test_planted_decay_curve_is_recovered_within_tolerance, test_half_life_refuses_when_ic_does_not_decay, test_decay_refuses_when_no_horizon_has_a_completed_forward_window, test_decay_refuses_mixed_fingerprints_without_override, test_decay_ignores_the_legacy_flat_frozen_panel, test_decay_cli_args_are_registered_on_the_subparser, test_decay_artifact_manifest_hashes_every_emitted_file, test_decay_markdown_reports_curve_half_life_power_and_trial_charge, test_zero_dispersion_trial_set_reports_that_nothing_was_deflated.
- [ ] `pytest -q tests/test_backtest.py tests/test_cli.py tests/test_significance.py tests/test_research_manifest.py tests/test_vault.py` still passes unchanged — M2 adds to backtest.py's callers and vault.py's surface but changes neither module's existing behaviour.
- [ ] `python -c "from stock_grader.cli import build_parser; a=build_parser().parse_args(['decay','--vault','v','--horizons','5','21','63','126','--primary-horizon','21','--ledger','l.jsonl','--format','json']); print(a.horizons, a.primary_horizon, a.ledger)"` prints `[5, 21, 63, 126] 21 l.jsonl` — proving subcommand-first parsing works (DOA-workflow guard).
- [ ] `python -c "from stock_grader.cli import build_parser; build_parser().parse_args(['decay','--vault','v','--asof','2026-01-01'])"` exits non-zero with an unrecognized-argument error — confirming no `--asof` was introduced.
- [ ] `stock-grader decay --vault C:/Users/tforstrom/Desktop/Stock-Vault --frozen-dir C:/Users/tforstrom/Desktop/Stock-Grader/frozen_scores --profile all_weather --primary-horizon 21 --allow-unverified-panel --ledger <scratch>/l.jsonl` exits 2 with a message naming the archive end date (2026-07-28) and the number of additional sessions the shortest horizon needs — the correct real-data behaviour today — and appends NOTHING to any ledger.
- [ ] On a synthetic-vault fixture run: `signal_decay/all_weather/<archive_through>/` contains exactly manifest.json, panel_h005.parquet, panel_h021.parquet, panel_h063.parquet, panel_h126.parquet, decay.json, decay.md; `manifest.json["schema_version"] == "1.0"`; every listed file's on-disk sha256 matches its manifest entry; `manifest.json["license_note"]` contains "PRIVATE".
- [ ] Each `panel_hNNN.parquet` loaded alone satisfies `evaluate_walk_forward(pd.read_parquet(p))` without raising, and `pd.concat([panel_h005, panel_h021])` raises ValueError matching "mixes return windows" or "duplicate" — the one-panel-per-horizon layout is demonstrated to be forced, not chosen.
- [ ] After one 4-horizon sweep against a scratch ledger seeded with K prior finite-Sharpe records: `load_manifest(ledger)` has K+4 lines; the 4 new records have `horizons == [[5],[21],[63],[126]]`; all four carry the identical `trials == K+4`; exactly one has `gate_passed` possibly True and its `horizons == [21]`; the other three have `gate_passed is False` and verdicts starting `"EXPLORATORY"`; `verify_chain(load_manifest(ledger))` is True and `all(verify_line(r) ...)` is True.
- [ ] `decay.md` contains, verifiably by substring: the not-investment-advice line; `config_fingerprint` and `universe_fingerprint` prefixes; a per-horizon table row for each horizon with mean rank IC and IC/sqrt(d); at least one ASCII bar line; the string "PRIMARY"; the sentence about E[max] assuming independent trials and therefore over-deflating correlated horizons; and either a half-life figure or one of the three explicit refusal notes.
- [ ] `git status --porcelain` after a real (or fixture) run shows NO untracked `signal_decay/` or `retro_scores/` paths, and `git check-ignore -q signal_decay` returns 0.
- [ ] `git diff` on `src/stock_grader/backtest.py` is empty — the decay curve is built entirely from `evaluate_walk_forward`'s existing outputs; no second rank-IC implementation was introduced and `validation_stats.rank_ic` remains unimported.
- [ ] `ruff check src tests scripts` and `mypy src/stock_grader/decay.py` are clean at the repo's configured settings (line-length 100).
- [ ] `docs/SIGNAL-DECAY.md` exists, is linked from README's docs list, and states the entry/exit session convention, the overlap dual-view rule, the ledger charging model, and the four data limits; `docs/REVIEW_FEEDBACK.md`'s Agent log has a one-line M2 entry with the commit hash.
