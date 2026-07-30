# M4 — Auxiliary signal panels: distil Stock-Vault collector archives into PIT-clean monthly signal panels evaluable by stock-grader backtest

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

**Why it matters:** Turn the vault's four restricted-license archives (IB borrow, FINRA short interest, Finnhub recommendations, SSGA holdings) from write-only archives into evaluable signals by emitting panels that satisfy the exact column contract `stock_grader.backtest._validate_panel` enforces, so `stock-grader backtest <panel.parquet>` runs on them unchanged. The FINRA panel is evaluable TODAY (measured: 5,890 names survive liquidity filters for settlement 2026-05-29, and market_eod covers 501 trading days 2024-07-29..2026-07-28, so widening the FINRA `--since` yields ~45 non-overlapping periods immediately); the other three accrue forward. This is the first place in the ecosystem where a non-fundamental signal can be measured against an out-of-sample cross-section, which is the whole point of the edge hunt.

## Prerequisites

- HANDOFF item 4 (ticker canonicalization) — this spec implements the Stock-Vault half of it (src/stock_vault/tickers.py) and can proceed before the Stock-Grader half lands, but the two must agree that canonical = SEC dash form
- HANDOFF item 5 (events.jsonl into the manifest contract + universe(asof)) — already partially done (Stock-Data/data/symbols/events/{events.jsonl,manifest.json} exist), but its earliest event is 2026-07-29, so a genuinely point-in-time universe (universe_is_pit=true) is blocked until roughly a year of events accrue
- FINRA archive backfill (step 2) must run before the short_interest panels have enough periods to evaluate
- ~~The in-flux multi-profile freeze work and the paper.py benchmark/fill journaling~~ **DONE (5290a2f / b407524)**: both are merged and pushed, so both files are safe to read and edit normally

## Verified ground truth

Every line below was confirmed by reading the cited code or data file. Re-verify anything that looks
stale before relying on it — and if the code contradicts this document, the code wins: say so in your
report rather than bending the code to match the doc.

- Vault archives on disk are exactly: data/borrow/<YYYY-MM>/usa_<YYYYMMDD>T<HHMM>.jsonl.gz, data/finra_short_interest/shrt<YYYYMMDD>.csv, data/rec_trends/finnhub_<YYYY-MM>.jsonl.gz, data/ssga_holdings/<YYYY-MM-DD>/<fund>.xlsx, data/market_eod/<YYYY-MM>/<YYYY-MM-DD>.jsonl.gz, data/delisted_prices/<YYYY>/<SYM>.json.gz + cohort_index.json, data/paper_journal/<YYYY-MM>.jsonl.gz. Every dataset dir carries manifest.json. There is no data/signal_panels/ yet.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/ (directory listing)`)*
- backtest panels REQUIRE exactly these columns: signal_date, return_start, return_end, ticker, score, forward_return. Optional recognized columns: filed_through (must be <= signal_date or ValueError), universe_is_pit, return_is_total, delisting_return_included, and a permanent id from the ordered list ('cik','security_id','permanent_id') — a candidate column is skipped if ANY value is NaN or blank.  
  *(`Stock-Grader/src/stock_grader/backtest.py:113-170 (_validate_panel, _attested, _permanent_id_column, _input_contract)`)*
- A signal_date whose rows carry more than one distinct return_start or return_end raises `ValueError(f"signal_date {…} mixes return windows")`. Truncating the window for a delisted name would trip this.  
  *(`Stock-Grader/src/stock_grader/backtest.py:276-282 (evaluate_walk_forward)`)*
- backtest rejects a whole signal_date when len(group) < config.min_cross_section (default 20) or score.nunique() < 2, and raises if NO period qualifies: 'no period met the minimum cross-section and score-dispersion requirements'.  
  *(`Stock-Grader/src/stock_grader/backtest.py:271-274, 318-321`)*
- forward_return < -1.0 raises. Non-finite score/forward_return rows are silently dropped before evaluation.  
  *(`Stock-Grader/src/stock_grader/backtest.py:158-163`)*
- IB borrow rows have exactly these keys: available, available_capped, con_id, currency, fee_rate, isin, name, rebate_rate, symbol. Measured on usa_20260729T2317.jsonl.gz: 19,719 rows; currency USD 19,685 / EUR 23 / CAD 11; 762 rows available_capped=true; fee_rate is annualized PERCENT (AAPL 0.2782, median 1.1063, max 1076.6441); 18,766 non-null fee_rate.  
  *(`Stock-Vault/data/borrow/2026-07/usa_20260729T2317.jsonl.gz; parser at src/stock_vault/borrow.py:parse_usa_txt`)*
- IB uses a SPACE class-share form: 'BRK A', 'BRK B', 'AAC U', 'ABR PRD' (453 symbols contain a space). market_eod (Polygon/Massive) uses the DOT form: 'BRK.A', 'BRK.B' (89 symbols contain a dot). SEC/canonical uses the DASH form 'BRK-B'.  
  *(`Stock-Vault/data/borrow/2026-07/usa_20260729T2317.jsonl.gz and data/market_eod/2026-07/2026-07-28.jsonl.gz (measured)`)*
- FINRA symbolCode uses NO separator at all for class shares: 'BRKA', 'BRKB'. This is a FIFTH symbology not covered by ticker_variants().  
  *(`Stock-Vault/data/finra_short_interest/shrt20260715.csv (rows for Berkshire Hathaway Inc. / BERKSHIRE HATHAWAY Class B)`)*
- FINRA CSV is pipe-delimited with header: accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|changePercent|changePreviousNumber|settlementDate. shrt20260715.csv has 22,375 data rows; marketClassCode counts OTC 9491, NNM 3836, NYSE 2906, ARCA 2665, SC 1654, BZX 1517, AMEX 306; stockSplitFlag=='S' on 70 rows; revisionFlag=='R' on 1 row.  
  *(`Stock-Vault/data/finra_short_interest/shrt20260715.csv (measured)`)*
- FINRA's own daysToCoverQuantity is FLOORED at 1.00 and CAPPED at 999.99 (e.g. BRKO has averageDailyVolumeQuantity 0 and daysToCoverQuantity 999.99; many illiquid names show exactly 1.00). It is unusable as a continuous cross-sectional variable.  
  *(`Stock-Vault/data/finra_short_interest/shrt20260715.csv (BRKO, BRKH, BRKHU, BRKHW rows)`)*
- Eight FINRA settlement files exist on disk: 2026-03-31, 04-15, 04-30, 05-15, 05-29, 06-15, 06-30, 07-15. All eight settlement dates are trading days present in market_eod.  
  *(`Stock-Vault/data/finra_short_interest/ (listing) cross-checked against data/market_eod/*/ filenames`)*
- finra.fetch(vault_dir, since, until=None) probes mid-month + month-end business-day candidates and ignores 404s, so widening --since backfills history without code changes. The CLI arg is `--since` (dt.date.fromisoformat), default now-120 days.  
  *(`Stock-Vault/src/stock_vault/finra.py:fetch / candidate_settlement_dates; src/stock_vault/cli.py (finra-short-interest subparser)`)*
- Finnhub recs record shape is {"ticker": <requested>, "snapshot_month": "YYYY-MM", "rows": [{"buy","hold","period","sell","strongBuy","strongSell","symbol"}, …]} with exactly 4 monthly periods, newest == first of snapshot month. 82 tickers in finnhub_2026-07.jsonl.gz. Only ONE snapshot month exists on disk.  
  *(`Stock-Vault/data/rec_trends/finnhub_2026-07.jsonl.gz (measured)`)*
- DATA-INTEGRITY TRAP: for requested ticker 'BRK-B' Finnhub returned rows whose inner symbol field is 'BRK.A' — the wrong security. Exactly 1 of 82 records has rows[].symbol != ticker. Analyst totals per current period range 10..(median 34.5).  
  *(`Stock-Vault/data/rec_trends/finnhub_2026-07.jsonl.gz (measured)`)*
- SSGA XLSX layout (openpyxl 0-based rows): sheet name 'holdings'; row0 ('Fund Name:', <name>); row1 ('Ticker Symbol:', 'SPY'); row2 ('Holdings:', 'As of 28-Jul-2026') — the as-of date, one day BEFORE the directory date 2026-07-29; row3 blank; row4 header ('Name','Ticker','Identifier','SEDOL','Weight','Sector','Shares Held','Local Currency'); data from row5. 'Identifier' is the CUSIP (AAPL = 037833100). 12 funds: spy, xlk, xlf, xle, xlv, xli, xlp, xly, xlu, xlb, xlre, xlc. Only ONE snapshot directory exists (2026-07-29).  
  *(`Stock-Vault/data/ssga_holdings/2026-07-29/spy.xlsx, xlk.xlsx, xlre.xlsx (read with openpyxl); FUNDS tuple at src/stock_vault/ssga.py`)*
- market_eod rows have keys: symbol, open, high, low, close, volume, vwap, transactions. 501 day files spanning 2024-07-29..2026-07-28 across 25 monthly manifests. 12,482 rows on 2026-07-28, zero null closes. Collected with adjusted=false, i.e. UNADJUSTED for splits and dividends.  
  *(`Stock-Vault/src/stock_vault/market_eod.py:fetch_day (params adjusted="false"); data/market_eod/ (measured)`)*
- MEASURED JOIN VIABILITY: squashing all separators ('.', '-', ' ') from market_eod's 12,435 symbols on 2026-07-15 produces 12,435 distinct keys — ZERO collisions. FINRA non-OTC rows for that settlement: 12,884; 11,884 join by exact symbol, 11,964 join via the squashed key (+80 class shares). Borrow: 10,938 of 19,719 rows join to market_eod via the squashed key.  
  *(`prototype run over Stock-Vault/data/finra_short_interest/shrt20260715.csv, data/market_eod/2026-07/2026-07-15.jsonl.gz, data/borrow/2026-07/usa_20260729T2317.jsonl.gz`)*
- MEASURED PANEL FEASIBILITY: for signal 2026-05-29, entry 2026-06-01, exit 2026-06-15, with filters close>=$5 and close*volume>=$1M, a FINRA DTC panel yields 5,890 rows (3,657 with a resolvable CIK = 62%); DTC median 2.14, p95 9.47; forward returns range -0.868..+0.738 with ZERO |return|>1.  
  *(`prototype run over Stock-Vault/data (finra + market_eod) and Stock-Data/data/symbols/current/sec_company_tickers.jsonl`)*
- CIK map source is one JSON object per line: {"cik": 1750, "ticker": "AIR", "title": "AAR CORP"} — 10,426 entries, cik is an INT (not zero-padded). Its manifest carries snapshot_date 2026-07-30, i.e. it is a CURRENT snapshot, not point-in-time.  
  *(`Stock-Data/data/symbols/current/sec_company_tickers.jsonl and .../manifest.json`)*
- Stock-Data/data/symbols/events/events.jsonl now exists WITH its own manifest.json (628 rows, rows format {"date","event":"added"|"removed","record":{cik,ticker,title},"source"}), but the earliest event date is 2026-07-29 — there is no usable PIT ticker history yet.  
  *(`Stock-Data/data/symbols/events/events.jsonl, manifest.json (last_checked_date 2026-07-30)`)*
- Stock-Data/data/corporate_actions/splits.jsonl contains only TWO rows, both AAPL; manifest tickers_requested is ["AAPL","JNJ","O"]. There is effectively NO split coverage for a whole-market panel.  
  *(`Stock-Data/data/corporate_actions/splits.jsonl and manifest.json`)*
- write_manifest(dataset_dir, *, source_urls, license_note, extra=None) hashes only files sitting DIRECTLY in dataset_dir (skips manifest.json, dotfiles, and subdirectories) and merges `extra` into the top level. SCHEMA_VERSION == '1.0'.  
  *(`Stock-Vault/src/stock_vault/manifest.py:write_manifest`)*
- Vault CLI uses a `shared` parent parser carrying --vault-dir (default env STOCK_VAULT_DIR or 'data') and every subcommand is registered with parents=[shared]; dispatch is a chain of `if args.command == ...` in main(), not set_defaults(func=…).  
  *(`Stock-Vault/src/stock_vault/cli.py:main`)*
- Stock-Vault runtime deps are ONLY requests>=2.31. pandas/pyarrow live in the `dev` and `paper` extras; openpyxl is NOT declared anywhere despite ssga.py downloading XLSX files.  
  *(`Stock-Vault/pyproject.toml [project].dependencies and [project.optional-dependencies]`)*
- staleness.py exposes reusable calendar helpers is_market_holiday(day) and previous_market_day(today), and a _CHECKS dict {'borrow','market-eod','recs','finra','paper'} exported as DATASETS.  
  *(`Stock-Vault/src/stock_vault/staleness.py:132-152, 265-270`)*
- VaultDataSource verifies every read against the dataset's manifest.json and refuses schema_version outside {'1.0'}; it exposes market_eod_day, market_eod_available_days, market_eod_series, borrow_latest, borrow_fee, delisted_history. There is NO reader for finra, recs, or ssga, and no panel builder anywhere in Stock-Grader.  
  *(`Stock-Grader/src/stock_grader/data/vault.py (full file); grep for 'forward_return' shows no builder`)*
- cmd_backtest appends a ResearchRecord to --ledger (default research_ledger.jsonl in the Stock-Grader repo root) on EVERY run, storing per_period_sharpe, mean_net_spread, mean_rank_ic and deflated_sharpe, and deflates by every trial the ledger has ever seen.  
  *(`Stock-Grader/src/stock_grader/cli.py:744-860 (cmd_backtest)`)*
- ticker_variants(ticker) returns only (as-given, dot->dash, dash->dot). It does NOT produce the IB space form or the FINRA squashed form. HANDOFF item 4 asks for a ~20-line canonicalization helper COPIED into Stock-Vault (no cross-repo imports).  
  *(`Stock-Grader/src/stock_grader/data/symbols.py:13-23; Stock-Grader/docs/HANDOFF.md §4`)*
- Frozen grader panels are now at frozen_scores/<profile>/<YYYY-MM-DD>.parquet (observed: frozen_scores/all_weather/2026-07-30.parquet) with cik as a zero-padded 10-char STRING. PROFILE_SPECS has 11 profiles: all_weather, deep_value, dividend_growth, dividend_income, garp, growth, low_volatility, momentum, quality, turnaround, value.  
  *(`Stock-Grader/frozen_scores/all_weather/2026-07-30.parquet; src/stock_grader/profiles.py:PROFILE_SPECS`)*
- paper.py establishes the sanctioned cross-repo pattern: a local clone PATH argument (--panel-source), with URL sources explicitly refused ('URL panel sources are not implemented; pass a local Stock-Grader clone path'). paper-trader.yml gets it via a second actions/checkout@v4 with path: grader.  
  *(`Stock-Vault/src/stock_vault/paper.py:load_frozen_panel; .github/workflows/paper-trader.yml`)*
- License notes already recorded per source: IBKR 'private research archive, publish derived aggregates only'; FINRA 'non-commercial internal use only; redistribution prohibited'; Finnhub 'no redistribution of data OR derived results'; SSGA 'reproduction clause; private research archive only'; Massive 'read current terms before any redistribution'.  
  *(`Stock-Vault/README.md License notes section; LICENSE_NOTE constants in borrow.py, finra.py, recs.py, ssga.py, market_eod.py`)*
- data/** is marked -text in .gitattributes so committed blobs stay byte-identical to what manifest sha256s describe. Vault data/ IS committed (the repo is private); .gitignore does not exclude data/.  
  *(`Stock-Vault/.gitattributes, .gitignore`)*

## Implementation steps

### Step 1. Declare the new dependencies and a `panels` extra

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/pyproject.toml`

In Stock-Vault/pyproject.toml add a new optional-dependency group: panels = ["pandas>=2.0", "pyarrow>=14", "openpyxl>=3.1"]. Add openpyxl>=3.1 to the existing `dev` group too so tests can build XLSX fixtures. Do NOT add pandas to [project].dependencies — the daily collectors.yml runs `pip install -e .` and must stay lean. openpyxl is currently undeclared even though ssga.py downloads XLSX; this fixes that gap.

### Step 2. Backfill FINRA history so the panel has periods to evaluate (network step, run FIRST — it gates everything measurable)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/data/finra_short_interest/`

Run: `stock-vault finra-short-interest --vault-dir C:/Users/tforstrom/Desktop/Stock-Vault/data --since 2024-07-01`. market_eod starts 2024-07-29, so 2024-07-01 is the useful floor. finra.fetch probes mid-month and month-end business days and ignores 404s, so this is safe and self-limiting. Expect roughly 45-50 new shrt*.csv files (8 exist today). If the CDN does not serve dates that old, record the actual earliest served date in docs/SIGNAL-PANELS.md — do not fabricate coverage. Commit the new files (they are ~2MB each; the repo is private and data/ is committed).

### Step 3. Create src/stock_vault/tickers.py — the vault's own symbology helper (HANDOFF item 4's 'copy the helper, no cross-repo imports')

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/tickers.py`

Pure module, no deps. Canonical form = SEC dash form, uppercase, stripped.

  def canonical(sym: str) -> str: upper/strip, then replace '.' and ' ' with '-', collapse repeated '-'.
  def squash(sym: str) -> str: upper/strip, then remove all of '.', '-', ' '.
  def variants(sym: str) -> tuple[str, ...]: ordered de-duplicated (as-given, dash, dot, space, squashed).
  def build_squash_index(symbols: Iterable[str]) -> tuple[dict[str, str], set[str]]:
      returns (key -> single market_eod symbol, ambiguous_keys). Build defaultdict(set) keyed by squash(); any key mapping to >1 symbol is EXCLUDED from the index and returned in ambiguous_keys so callers can count/report it. Measured today: 12,435 market_eod symbols -> 12,435 keys, zero ambiguity — but the guard must exist because ticker reuse will eventually break it.

Join rule every source uses: squash the source symbol, look it up in the squash index built from that day's market_eod symbols, and carry BOTH the market_eod symbol (for price lookup) and canonical(market_eod symbol) (as the panel's `ticker`).

### Step 4. Create src/stock_vault/prices.py — market_eod calendar, ADV, forward returns, split guard, liquidity filter

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/prices.py`

Imports pandas lazily inside functions (the module must import without pandas so `pip install -e .` collectors keep working).

  trading_days(vault_dir) -> list[dt.date]: glob data/market_eod/*/*.jsonl.gz, parse stems, sorted. (501 today.)
  load_day(vault_dir, day) -> dict[str, dict]: gzip.open the day file, json.loads each line, key by row['symbol']. Cache in an LRU dict keyed by date, capped at ~64 days, or the 500-day scan will re-read files O(n^2).
  adv_shares(vault_dir, symbols, asof, lookback=20) -> dict[str, float]: MEDIAN of 'volume' over the last `lookback` trading days ending at and including asof. Median, not mean — Polygon volume has fat single-day spikes. Symbols missing on some days use whatever days they have; require >= lookback//2 observations or return 0.0.
  liquid_universe(bars, min_price=5.0, min_dollar_volume=1_000_000.0) -> set[str]: close is not None and close >= min_price and close*volume >= min_dollar_volume.
  suspect_corporate_action(vault_dir, symbol, start, end) -> bool: walk consecutive trading days in [start, end]; flag when close_t/close_{t-1} is within 3% of k or 1/k for k in (2,3,4,5,6,7,8,10,15,20) AND volume_t/volume_{t-1} > 1.5. market_eod is adjusted=false and Stock-Data's splits.jsonl has only 2 AAPL rows, so this heuristic is the ONLY split defence.
  forward_return(vault_dir, symbol, return_start, return_end) -> tuple[float|None, bool]:
      entry = close on return_start (None if the symbol has no bar that day -> return (None, False), row is dropped).
      exit  = close on return_end; if absent, walk BACKWARD through trading days to the last bar at or before return_end and set truncated=True (the delisting case).
      returns (exit/entry - 1.0, truncated).
      *** The return_start / return_end DATE COLUMNS MUST NOT CHANGE for truncated rows *** — backtest.py raises 'signal_date … mixes return windows' if a single signal_date carries more than one distinct return_start or return_end. Only the price lookup falls back, never the dates.

### Step 5. Create src/stock_vault/signals.py — six pure per-source extractors

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/signals.py`

Each extractor returns list[SignalObservation] where SignalObservation is a dataclass: signal_date (dt.date), source_symbol (str), security_id (str|None), signal_raw (float), filed_through (dt.date), source_asof (dt.date). No price data, no universe filtering, no sign flipping — those happen in panels.py. Definitions:

A) short_interest_dtc (FINRA, direction -1)
   For each shrt<YYYYMMDD>.csv: parse with csv.DictReader(delimiter='|'). Drop marketClassCode == 'OTC' (9,491 of 22,375 rows). signal_raw is computed in panels.py as si_shares / adv_shares because ADV comes from market_eod — the extractor emits si_shares = float(currentShortPositionQuantity) as signal_raw and panels.py divides. DO NOT use FINRA's daysToCoverQuantity: it is floored at 1.00 and capped at 999.99 (verified).
   PIT: settlement date S is NOT the knowable date — FINRA publishes on a lag. signal_date = filed_through = the S + `finra_lag_business_days` (CLI default 8) business day, rolled FORWARD to the next date present in trading_days(). source_asof = S.
   security_id = None (the file has no permanent identifier).
   Drop rows where stockSplitFlag == 'S' is set (70 rows in 2026-07-15) only for the CHANGE signal, not the level.

B) short_interest_change (FINRA, direction -1)
   Needs the prior archived settlement file. signal_raw_pre = float(currentShortPositionQuantity) - float(prior file's currentShortPositionQuantity for the same symbolCode); panels.py divides by adv_shares to yield dtc_change (a change in days-to-cover; no logs, so si == 0 is handled). Skip a symbol entirely when the prior file is absent — never fall back to previousShortPositionQuantity silently; if you use it as a fallback, emit a `prev_source` column recording which was used. Drop any row with stockSplitFlag == 'S' in EITHER file (share counts not comparable). Keep revisionFlag == 'R' rows but set a `revised` flag column.

C) borrow_fee_level (IB, direction -1)
   Snapshot selection for a candidate signal_date D: parse the UTC stamp from usa_<YYYYMMDD>T<HHMM>.jsonl.gz and take the LATEST snapshot with stamp <= D 20:00 UTC. Verified stamps are ~13:19 UTC (pre-open) and ~23:17 UTC (post-close), so this picks D's pre-open snapshot — knowable at D's close — and never a post-close one from D itself. filed_through = the chosen snapshot's UTC date; source_asof = same.
   Filter currency == 'USD' and fee_rate is not None. signal_raw = math.log1p(fee_rate / 100.0) — fee_rate is annualized PERCENT (AAPL 0.2782, median 1.1063, max 1076.6441).
   security_id = f"IBCON:{con_id}" — a genuinely permanent, in-file, PIT identifier (AAPL con_id 265598).
   source_symbol: IB's space form; the join uses squash().

D) borrow_fee_change (IB, direction -1)
   signal_raw = log1p(fee_D/100) - log1p(fee_{D-21 trading days}/100), using rule (C) to pick a snapshot at each end. Drop the observation if either end's `available_capped` is true (762 capped rows; availability is censored at 10M). The archive begins 2026-07-28, so the first computable signal_date is ~2026-08-27 — the builder must emit zero periods today WITHOUT raising.

E) rec_consensus_delta (Finnhub, direction +1)
   Per record, IGNORE the outer `ticker` and key on rows[].symbol — verified trap: requested 'BRK-B' returned rows whose symbol is 'BRK.A' (1 of 82 records). Emit a warning and record the mismatch count; drop mismatched records by default, with --recs-keep-symbol-mismatch to keep them keyed by rows[].symbol.
   consensus(row) = (2*strongBuy + 1*buy + 0*hold - 1*sell - 2*strongSell) / total, total = sum of the five counts; drop rows with total < 3 (verified minimum observed total is 10, so this only guards future thin coverage).
   signal_raw = consensus(period == first-of-snapshot-month) - consensus(period == first-of-previous-month), BOTH taken from the SAME snapshot file. This is PIT-clean: both numbers were visible on the snapshot date.
   *** Never build a history by treating each `period` row across one file as an observation made at that period — Finnhub restates the trailing 3 months. Only the newest period row of each snapshot is a fresh observation. ***
   signal_date = filed_through = the last trading day of snapshot_month (conservative and provable; the archive cannot prove the intra-month collection instant because write_manifest rewrites generated_at_utc on every run). Provide --recs-observation-day {month-end,cron-day} with month-end as the default; cron-day (the 2nd, rolled forward to a trading day) is only defensible for snapshots actually written by the monthly-recs cron.
   security_id = None; cik resolves for all 82 large caps.

F) etf_share_change (SSGA, direction +1)
   Parse with openpyxl (read_only=True, data_only=True), sheet 'holdings': as-of date from cell at row index 2 / col index 1, text 'As of 28-Jul-2026' -> strptime('%d-%b-%Y') after stripping 'As of '; header at row index 4; data from row index 5, STOPPING at the first row whose Ticker cell is blank (trailing disclaimer rows follow the holdings).
   Between consecutive monthly SSGA snapshot directories t_prev and t, per fund f: g_i = ln(shares_i(t) / shares_i(t_prev)); flow_f = median_i(g_i) (fund-level creation/redemption); signal_raw = g_i - flow_f. This isolates index/share-count change from AUM flow. Using WEIGHT change instead would be a mechanical price-momentum proxy — do not use weight.
   Aggregation when a stock is in several funds: prefer SPY's value; else the fund where the stock's Weight is largest. Deterministic tie-break by fund ticker.
   Drop names absent from either snapshot (index adds/drops are a different, binary signal — out of scope). Drop |signal_raw| > ln(1.5) as a suspected split (there is no split coverage to corroborate with).
   security_id = f"CUSIP:{Identifier}" — in-file, permanent, PIT-clean (AAPL 037833100).
   signal_date = filed_through = the snapshot DIRECTORY date rolled back to the previous trading day if it is not itself one; source_asof = the in-file 'As of' date (verified one day earlier than the directory date).
   Monthly cadence: use the LAST snapshot directory in each calendar month. Only one directory exists today -> zero periods; emit nothing and exit 0.

### Step 6. Create src/stock_vault/panels.py — assembly, universe join, forward returns, writers

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/panels.py`

SIGNALS registry: dict name -> SignalSpec(extractor, direction:int, periods_per_year:int, license_note:str, source_urls:list[str], horizon_default:str). periods_per_year: short_interest_* = 24 (twice-monthly), borrow_* / rec_consensus_delta / etf_share_change = 12.

Horizon (`--horizon`, default 'next-observation'):
  'next-observation' -> return_start = trading day AFTER signal_date; return_end = the trading day immediately BEFORE the next signal_date's return_start. Non-overlapping periods; this is what makes periods_per_year honest and keeps the bootstrap's iid-ish assumption defensible. The last signal_date (no successor) is dropped.
  '<int>' -> fixed N trading days: return_start = days[i+1], return_end = days[i+N]. For twice-monthly FINRA this OVERLAPS; print a loud warning, set manifest key overlapping_windows=true, and record it in the panel as a column so it cannot be forgotten.

Per signal_date:
  1. bars = prices.load_day(signal_date); index = tickers.build_squash_index(bars.keys()).
  2. universe = prices.liquid_universe(bars, min_price, min_dollar_volume).
  3. join each SignalObservation via squash(source_symbol) -> market_eod symbol; drop misses and ambiguous keys, counting both.
  4. require the symbol to have bars on signal_date AND return_start (else drop).
  5. forward_return via prices.forward_return (dates fixed, price lookup falls back for delistings).
  6. drop rows flagged by prices.suspect_corporate_action over [return_start, return_end]; drop |forward_return| > --max-abs-return (default 1.0). Count both in the manifest. (Measured: zero such rows in the 2026-05-29 -> 2026-06-15 FINRA window, so the guards are cheap.)
  7. score = direction * scipy-free cross-sectional rank: pandas Series.rank(method='average', pct=True) of signal_raw, then multiplied by direction. Sign-adjusting HERE (not at analysis time) is what makes the hypothesis pre-registered — a positive mean_rank_ic then means the prior was right, and there is exactly one hypothesis per signal.
  8. cik: load Stock-Data/data/symbols/current/sec_company_tickers.jsonl from --foundry-dir, build {squash(ticker): f"{cik:010d}"}; unresolved -> None. DEFAULT: keep unresolved rows (measured 62% resolution for FINRA — dropping the other 38% would inject survivorship bias into the universe, which is worse than losing the permanent-identifier attestation). Provide --require-cik to drop them, documented as survivorship-biasing.

Emitted columns, in this order:
  signal_date, return_start, return_end (all ISO date strings), ticker (canonical dash form), cik (str|None), security_id (str|None), score (float), signal_raw (float), forward_return (float), filed_through (ISO str), source_asof (ISO str), universe_is_pit (bool), return_is_total (bool), delisting_return_included (bool), truncated_window (bool), suspect_split (bool, always False after filtering — kept for auditability), signal_name (str), signal_direction (int), overlapping_windows (bool), horizon_trading_days (int), vault_commit (str, from `git rev-parse --short HEAD` with a subprocess fallback of 'unknown'), schema_version ('1.0').
  universe_is_pit = False for every panel: the market_eod cross-section IS point-in-time, but the CIK map is a current snapshot (snapshot_date 2026-07-30) and symbols/events only starts 2026-07-29. Emit the literal False, do not omit the column — an explicit false is the honest record.
  return_is_total = False and delisting_return_included = False always: market_eod is adjusted=false (no dividends, no split adjustment) and there are no delisting proceeds anywhere in the vault.

Output layout — data/signal_panels/<signal_name>/ containing:
  <signal_date>.parquet   immutable per-period part; NEVER overwritten (skip with a log line if present unless --rebuild)
  panel.parquet           rollup: concat of every part, sorted by (signal_date, ticker); rebuilt every run
  manifest.json           write_manifest(dir, source_urls=spec.source_urls, license_note=spec.license_note, extra={...})
Keep parts FLAT in the dataset dir (not a parts/ subdir) — write_manifest only hashes files sitting directly in dataset_dir.
manifest extra keys: signal, definition (one-sentence prose), direction, periods_per_year, horizon, overlapping_windows, pit_rule (prose describing the filed_through derivation), universe_filters, periods, observations, dropped_no_price, dropped_ambiguous_symbol, dropped_suspect_split, dropped_extreme_return, truncated_windows, cik_resolution_rate, recs_symbol_mismatches, source_datasets (the vault dataset dirs read).

Path guard: assert the resolved output path is inside Path(vault_dir).resolve(); raise otherwise. These panels are restricted-license derivatives and must never be written into a Stock-Grader or Stock-Data checkout.

### Step 7. Wire the CLI subcommand

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/cli.py`

In Stock-Vault/src/stock_vault/cli.py add, using the existing pattern:
  panel = sub.add_parser('signal-panel', parents=[shared], help='build PIT signal panels from the vault archives')
  panel.add_argument('--signal', required=True, choices=sorted(panels.SIGNALS) + ['all'])
  panel.add_argument('--foundry-dir', default=os.environ.get('STOCK_DATA_DIR'), help='local Stock-Data clone (for the ticker->CIK map); omit to emit null CIKs')
  panel.add_argument('--horizon', default='next-observation')
  panel.add_argument('--min-price', type=float, default=5.0)
  panel.add_argument('--min-dollar-volume', type=float, default=1_000_000.0)
  panel.add_argument('--max-abs-return', type=float, default=1.0)
  panel.add_argument('--finra-lag-business-days', type=int, default=8)
  panel.add_argument('--recs-observation-day', choices=['month-end','cron-day'], default='month-end')
  panel.add_argument('--require-cik', action='store_true')
  panel.add_argument('--rebuild', action='store_true', help='recompute existing per-date parts (default: skip)')
  panel.set_defaults() -- then add `if args.command == 'signal-panel':` to main()'s dispatch chain, printing per-signal {periods, observations} and returning 0 even when a signal yields zero periods (borrow_fee_change, etf_share_change, rec_consensus_delta all legitimately yield zero today).
--vault-dir MUST come from parents=[shared] on the SUBPARSER (HANDOFF working rule 3: subcommand-first args registered only at top level silently killed scheduled runs in two repos).

### Step 8. Add a staleness clock for the derived panels

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/staleness.py`

In staleness.py add check_signal_panels(vault_dir, now=None): for each subdirectory of data/signal_panels/, read manifest.json, parse generated_at_utc via the existing _parse_manifest_timestamp, and raise StalenessError when it is older than 40 days (monthly cadence + slack). Bootstrap-tolerant: if data/signal_panels/ does not exist, return a 'bootstrap' message rather than raising. Register as _CHECKS['signal-panels'] so it joins DATASETS automatically and becomes a valid `stock-vault check-staleness` choice.

### Step 9. Tests — tests/test_panels.py (new file, follows the existing pure-parsing style of tests/test_collectors.py)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/tests/test_panels.py`

Build a tiny synthetic vault in tmp_path (write real gzipped jsonl for market_eod and borrow, a real pipe-delimited CSV for FINRA, a real openpyxl-written XLSX for SSGA, and real manifests via manifest.write_manifest) and assert:
  test_squash_index_drops_ambiguous_keys — two symbols squashing to the same key are both excluded and reported.
  test_ib_space_and_finra_squashed_symbols_join_to_market_eod — 'BRK B' and 'BRKB' both resolve to market_eod 'BRK.B' and canonicalize to panel ticker 'BRK-B'.
  test_finra_signal_date_is_settlement_plus_publication_lag — a settlement on a known date produces signal_date 8 business days later, rolled to a trading day, and filed_through == signal_date.
  test_finra_dtc_ignores_vendor_days_to_cover — a row with daysToCoverQuantity '999.99' and ADV 0 is dropped, and a row with daysToCoverQuantity '1.00' still gets a computed DTC != 1.0.
  test_truncated_delisting_keeps_uniform_return_end — a symbol whose last bar precedes return_end still carries the panel-wide return_end string, and evaluate_walk_forward-style grouping sees exactly one return window (assert the panel's groupby('signal_date')[['return_start','return_end']].nunique() is all 1).
  test_recs_uses_row_symbol_not_requested_ticker — a record with ticker 'BRK-B' and rows[].symbol 'BRK.A' is dropped by default and counted in the manifest.
  test_recs_delta_is_within_snapshot — the delta uses two period rows from one file, and a second snapshot month does not retroactively change the first month's part file.
  test_ssga_share_change_is_flow_neutral — a fund where every holding's share count rises 10% yields signal_raw ~ 0 for all names.
  test_split_guard_drops_suspect_window — a synthetic 1:4 price drop with a 2x volume jump is dropped and counted.
  test_parts_are_immutable — a second run without --rebuild leaves the part file's bytes unchanged.
  test_panel_refuses_to_write_outside_vault_dir.
  test_emitted_panel_satisfies_backtest_contract — replicate backtest._validate_panel's required-column set and its filed_through <= signal_date / return_start > signal_date / return_end > return_start / no duplicate (signal_date, ticker) invariants IN THE TEST (do NOT import stock_grader — cross-repo imports are forbidden by ECOSYSTEM rule 1).
Run: `cd C:/Users/tforstrom/Desktop/Stock-Vault && pip install -e ".[dev,panels]" && python -m pytest -q`

### Step 10. Monthly workflow

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/.github/workflows/monthly-panels.yml`

New .github/workflows/monthly-panels.yml, modelled on monthly-recs.yml:
  on: schedule: - cron: "29 13 16 * *"   (16th of each month — by then the previous month-end FINRA settlement has cleared its ~8-business-day publication lag) ; workflow_dispatch
  permissions: contents: write ; concurrency: group: monthly-panels
  steps: actions/checkout@v4 (vault) ; actions/checkout@v4 with repository: TylerJForstrom/Stock-Data, path: foundry ; setup-python 3.12 ; `pip install -e .[panels]` ; pre-collection staleness gate `stock-vault check-staleness --vault-dir data finra borrow market-eod` (bootstrap-guard signal-panels the way collectors.yml guards paper) ; `stock-vault signal-panel --vault-dir data --foundry-dir foundry --signal all` ; commit with the same 3-attempt push-rebase loop used by the other three workflows.
Do NOT fold this into collectors.yml: the builder reads all 501 market_eod day files and would add minutes to the daily job.
After merging, dispatch it once and VERIFY completion with `gh run list --repo TylerJForstrom/Stock-Vault --limit 5` (HANDOFF working rule 5 — a dispatched-but-dead run already burned a day of archive).

### Step 11. Documentation: the licensing matrix and the operator workflow

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/docs/SIGNAL-PANELS.md`, `C:/Users/tforstrom/Desktop/Stock-Vault/README.md`

Create Stock-Vault/docs/SIGNAL-PANELS.md containing:
  (a) the six signal definitions verbatim (formula, direction, PIT rule, expected cadence, first computable signal_date);
  (b) THE LICENSING MATRIX, stated as may/may-not:
      MAY be published: the signal DEFINITIONS, the builder CODE, this document, and the per-signal manifest key `definition`. Ideas and code are not the licensed data.
      MUST stay in Stock-Vault (private) and MUST NEVER be written into Stock-Grader or Stock-Data: every *.parquet under data/signal_panels/, every raw archive it reads, and any per-security value derived from them (scores, ranks, DTC, fee levels, share changes).
      Backtest OUTPUT metrics: IBKR permits derived AGGREGATES, so borrow_* aggregate diagnostics (mean rank IC, net spread, Sharpe) MAY be published with attribution. FINRA (redistribution prohibited) and SSGA (reproduction clause) aggregates are kept private as the conservative reading. Finnhub explicitly forbids redistribution of data OR DERIVED RESULTS — rec_consensus_delta backtest metrics MUST NOT be published anywhere, including the public research ledger. Massive/Polygon terms are unverified, so forward-return-derived numbers inherit the strictest constraint in the panel.
      Consequence for the trial ledger: restricted-panel backtests MUST be run with `--ledger C:/Users/tforstrom/Desktop/Stock-Vault/data/research_ledger.jsonl` (a PRIVATE ledger), never the public Stock-Grader research_ledger.jsonl. Document loudly that deflated-Sharpe correction is per-ledger, that the private ledger therefore carries its own independent trial count, and that choosing whichever ledger flatters a result is exactly the fraud the SHA-256 chain exists to expose.
  (c) the operator's monthly loop:
      1. workflows run (collectors daily, monthly-recs on the 2nd, monthly-panels on the 16th);
      2. `git -C Stock-Vault pull`;
      3. `stock-grader backtest C:/Users/tforstrom/Desktop/Stock-Vault/data/signal_panels/short_interest_dtc/panel.parquet --periods-per-year 24 --min-cross-section 200 --allow-unverified-panel --ledger C:/Users/tforstrom/Desktop/Stock-Vault/data/research_ledger.jsonl --format md` (periods-per-year comes from the panel's manifest; --allow-unverified-panel is REQUIRED because universe_is_pit / return_is_total / delisting_return_included are all honestly false);
      4. paste the markdown into the private log; never into a public repo for the Finnhub panel.
Also add a row per panel to Stock-Vault/README.md's Collectors table and a `signal_panels` line to its License notes section.

### Step 12. Record the ecosystem decision in the contract

*Files:* `C:/Users/tforstrom/Desktop/Stock-Data/ECOSYSTEM.md`

Append to Stock-Data/ECOSYSTEM.md's Decision log: '2026-07-30: Auxiliary signal panels are built IN Stock-Vault (restricted-license derivatives must never enter a public repo) and consumed by Stock-Grader as a local file path passed to `backtest` — artifacts, not imports. Panel schema is the documented backtest panel contract plus additive provenance columns. Finnhub-derived backtest metrics are non-publishable and use the private vault ledger.' Also add the canonical-symbology line HANDOFF item 4 asks for if it is still absent: canonical form is the SEC dash form; adapters canonicalize on read; the squashed form (all separators removed) is the join key of last resort and must be rejected when it is ambiguous.

### Step 13. Run the real build and record measured reality

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/data/signal_panels/`, `C:/Users/tforstrom/Desktop/Stock-Vault/docs/SIGNAL-PANELS.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/REVIEW_FEEDBACK.md`

Locally: `stock-vault signal-panel --vault-dir C:/Users/tforstrom/Desktop/Stock-Vault/data --foundry-dir C:/Users/tforstrom/Desktop/Stock-Data --signal all`. Expect short_interest_dtc and short_interest_change to produce periods (one per archived settlement file minus the last, minus the publication-lag roll), and borrow_fee_change / etf_share_change / rec_consensus_delta to produce zero periods without error. Then run the backtest command from the docs and paste the resulting period count, mean rank IC, and lifetime trial count into docs/SIGNAL-PANELS.md under a 'First run' heading — measured numbers, not predicted ones. Log one line per completed item in Stock-Grader/docs/REVIEW_FEEDBACK.md's Agent log with commit hashes, per that repo's AGENTS.md.

## Pitfalls

- MIXED RETURN WINDOWS: backtest.py raises `ValueError: signal_date <d> mixes return windows` if one signal_date carries more than one distinct return_start or return_end. The delisting fallback must change only the PRICE lookup (last bar at or before return_end), never the date columns. This is the single easiest way to make every panel unusable.
- FINRA SETTLEMENT DATE IS NOT THE KNOWABLE DATE. Using settlementDate as signal_date leaks roughly 8 business days of future information into every observation and would make the whole panel worthless. signal_date must be settlement + publication lag, and filed_through must equal it so backtest's `filed_through > signal_date` check actively guards the invariant.
- FINNHUB RESTATES. Each snapshot file contains 4 monthly `period` rows and the trailing 3 are restatements. Building a time series by reading period fields out of one file is textbook lookahead. Only the newest period row per snapshot is a fresh observation; the within-snapshot delta is legitimate only because BOTH of its inputs were visible on the snapshot date.
- FINNHUB RETURNS THE WRONG SECURITY. Verified: requested ticker 'BRK-B' came back with rows[].symbol == 'BRK.A'. Key on rows[].symbol, count mismatches, and surface them — silently trusting the outer `ticker` mislabels a security's entire history.
- FINRA's daysToCoverQuantity is floored at 1.00 and capped at 999.99 (verified: BRKO shows 999.99 with ADV 0; dozens of illiquid names show exactly 1.00). Using it as the DTC signal makes the bottom half of the cross-section a constant and destroys the rank IC. Compute DTC from currentShortPositionQuantity and your own median ADV from market_eod.
- FIVE SYMBOLOGIES, NOT FOUR. HANDOFF item 4 lists SEC 'BRK-B', Polygon 'BRK.B', IB 'BRK B', TickerPulse 'BRK.B' — FINRA adds a fifth with NO separator at all: 'BRKB'. ticker_variants() in Stock-Grader covers none of the last two. The vault needs its own helper (no cross-repo imports) and the squashed key must be rejected when ambiguous, even though today's market_eod cross-section happens to have zero collisions.
- market_eod IS UNADJUSTED (fetch_day passes adjusted="false") and Stock-Data's splits.jsonl has exactly TWO rows, both AAPL. There is no split coverage. Without the ratio+volume split heuristic and the |return| cap, a single 1:10 reverse split becomes a fake +900% forward return that dominates a whole quantile bucket.
- DROPPING NAMES WITHOUT A CIK INJECTS SURVIVORSHIP BIAS. Measured CIK resolution is 62% for the FINRA cross-section, and the map (symbols/current, snapshot_date 2026-07-30) is a CURRENT snapshot: a company delisted in 2025 is simply absent, so requiring CIK would silently delete exactly the losers. Keep unresolved rows, accept permanent_identifier_present=False, and say so.
- OVERLAPPING RETURN WINDOWS INFLATE SHARPE. FINRA is twice-monthly; a fixed 21-trading-day horizon overlaps consecutive periods, autocorrelating the net-spread series that per_period_sharpe and the moving-block bootstrap both assume is roughly independent. Default to non-overlapping 'next-observation' windows and set periods_per_year=24 for the FINRA panels, 12 for the monthly ones.
- EVERY BACKTEST RUN IS A LEDGER TRIAL. cmd_backtest appends a ResearchRecord unconditionally and deflates by every trial the ledger has ever seen. Six signals x a couple of horizon choices is a dozen trials; expect the deflated Sharpe to be brutal, and do not delete the ledger to reset the count. Restricted-source trials go to the PRIVATE vault ledger — a Finnhub-derived mean_rank_ic in Stock-Grader's public research_ledger.jsonl is a licence breach that git history makes permanent.
- ARGPARSE PLACEMENT: --vault-dir must be registered on the signal-panel subparser via parents=[shared], matching every other vault subcommand. The top-level-only variant is the documented 'DOA workflow' bug class that silently killed scheduled runs in two repos.
- NEVER WRAP A DOCUMENTED SCHEMA IN AN ENVELOPE. Provenance columns are ADDITIVE to the six required panel columns; manifest keys are additive via write_manifest's `extra`. HANDOFF records this mistake being made twice in one week.
- write_manifest only hashes files sitting DIRECTLY in the dataset directory (it skips subdirectories). Putting per-period parts in a parts/ subdir leaves them unhashed and, for anything read back through VaultDataSource._read_verified, unreadable.
- Stock-Vault's runtime dependency set is requests ONLY, and collectors.yml runs `pip install -e .` . Importing pandas/openpyxl at module scope in anything cli.py imports eagerly will break the daily collector run. Import them inside functions, exactly as paper.py:load_frozen_panel does.
- PowerShell 5.1 on this machine: no `&&`/`||` chaining, and never `2>$null` a native command. Use the Bash tool for POSIX-style scripts, and never pipe a secret to stdin (it appends CRLF — that artifact already produced HTTP 401s from a key+CRLF secret).
- Three of the six panels legitimately produce ZERO periods today (borrow archive starts 2026-07-28, one SSGA snapshot, one recs snapshot). The builder must log that and exit 0, not raise — and evaluate_walk_forward WILL raise 'no period met the minimum cross-section' if you hand it a one-period rollup, so the docs must tell the operator to wait rather than treat it as a bug.
- Vault data/** is marked `-text` in .gitattributes precisely so committed blobs match their manifest sha256s. Do not write panel parquet through any path that normalizes line endings, and do not add data/signal_panels to .gitignore — the private repo commits its data.

## Acceptance criteria

Do not report this milestone complete until every box is checkable.

- [ ] `cd C:/Users/tforstrom/Desktop/Stock-Vault && pip install -e ".[dev,panels]" && python -m pytest -q` is green, and tests/test_panels.py contributes at least the twelve named tests including test_truncated_delisting_keeps_uniform_return_end, test_recs_uses_row_symbol_not_requested_ticker, test_finra_dtc_ignores_vendor_days_to_cover, and test_panel_refuses_to_write_outside_vault_dir.
- [ ] `python -m ruff check src tests` is clean at the repo's line-length 100 with select = E,F,W,I,UP,B.
- [ ] `stock-vault signal-panel --help` shows --vault-dir (proving parents=[shared] registration), --signal, --foundry-dir, --horizon, --min-price, --min-dollar-volume, --max-abs-return, --finra-lag-business-days, --recs-observation-day, --require-cik, --rebuild.
- [ ] After `stock-vault signal-panel --vault-dir data --foundry-dir <Stock-Data clone> --signal all`, the directory C:/Users/tforstrom/Desktop/Stock-Vault/data/signal_panels/short_interest_dtc/ exists on disk and contains at least 6 per-date parquet parts, a panel.parquet, and a manifest.json whose schema_version is "1.0" and whose `files` list names every parquet with a matching sha256.
- [ ] panel.parquet for short_interest_dtc has exactly the columns signal_date, return_start, return_end, ticker, cik, security_id, score, signal_raw, forward_return, filed_through, source_asof, universe_is_pit, return_is_total, delisting_return_included, truncated_window, suspect_split, signal_name, signal_direction, overlapping_windows, horizon_trading_days, vault_commit, schema_version — and `pd.read_parquet(...).groupby('signal_date')[['return_start','return_end']].nunique().max().max() == 1`.
- [ ] Every row satisfies filed_through <= signal_date < return_start < return_end, and `df.duplicated(['signal_date','ticker']).any()` is False.
- [ ] `stock-grader backtest <vault>/data/signal_panels/short_interest_dtc/panel.parquet --periods-per-year 24 --min-cross-section 200 --allow-unverified-panel --ledger <vault>/data/research_ledger.jsonl --format md` exits 0 and prints a table with at least 5 accepted periods; the rendered Input contract shows filing cutoff provided = yes and point-in-time universe = NO (the honest state).
- [ ] Running the same backtest WITHOUT --allow-unverified-panel exits non-zero with the 'panel fails the strict input contract' message — proving the attestations are honestly false rather than fabricated true.
- [ ] The builder is re-runnable: a second `signal-panel --signal all` leaves every existing per-date part byte-identical (verify with `git status --porcelain data/signal_panels` showing only panel.parquet and manifest.json modified).
- [ ] `stock-vault check-staleness --vault-dir data signal-panels` exits 0 after a fresh build, and `signal-panels` appears in the subcommand's choices (i.e. it is in staleness.DATASETS).
- [ ] Stock-Vault/docs/SIGNAL-PANELS.md exists and contains an explicit may/may-not-publish table naming all five restricted sources, and states that Finnhub-derived backtest metrics must not enter Stock-Grader/research_ledger.jsonl.
- [ ] `git -C C:/Users/tforstrom/Desktop/Stock-Grader status --porcelain` shows NO new parquet or panel files — no restricted-license derivative has entered a public repo.
- [ ] monthly-panels.yml has been dispatched once and `gh run list --repo TylerJForstrom/Stock-Vault --limit 5` shows it completed successfully (dispatched-but-dead does not count).
