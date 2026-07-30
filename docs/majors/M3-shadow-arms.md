# M3 — Shadow paper arms: a deterministic 11-profile simulator sharing the real trader's pre-registered rules, journal shapes, and reader

> Part of the major-improvements handoff. Read [`../MAJOR_IMPROVEMENTS.md`](../MAJOR_IMPROVEMENTS.md)
> first — it carries the orientation, the working rules, and the milestone ordering.

**Effort:** large

**Why it matters:** Alpaca gives exactly one paper account, so the forward-evidence clock currently tests exactly one profile (all_weather) while the other ten style lenses accrue frozen panels monthly and no forward record at all. This adds a deterministic simulator in Stock-Vault that replays the IDENTICAL pre-registered v1 selection rules — by calling paper.target_portfolio, never reimplementing it — over each profile's frozen panels, filling at the next archived session close from the whole-market EOD archive under an explicit bps cost model, and writing one append-only journal per arm using the SAME kind/record shapes as the real account. That buys an out-of-sample, pre-registered, cross-profile horse race where 11 equity curves accumulate in parallel under one reader, plus a real-vs-simulated fill calibration that turns the single real account into a measurement instrument for the other ten.

## Prerequisites

- Stock-Grader multi-profile freeze (in flight, uncommitted): cmd_freeze writing frozen_scores/<profile>/YYYY-MM-DD.parquet and monthly-freeze.yml running `stock-grader freeze --all-profiles`. The simulator has nothing to replay for 10 of 11 arms until that lands and the first --all-profiles freeze runs. Only frozen_scores/all_weather/2026-07-30.parquet exists today.
- Stock-Vault paper.py benchmark/fill journaling (in flight): the {"kind":"fill"} record shape must be reconciled with the shadow's before either is frozen. Zero fill records exist on disk today, so calibrate() is exercisable only after the real account actually trades a panel.
- market_eod archive coverage: 25 month dirs (2024-07 .. 2026-07) each with manifest.json. Shadow arms can only be simulated over sessions present in this archive; the daily collectors.yml cron must keep running or arms stall.
- Step 1 (per-profile panel loader) blocks every other step AND unblocks the currently-broken real paper-rebalance.

## Verified ground truth

Every line below was confirmed by reading the cited code or data file. Re-verify anything that looks
stale before relying on it — and if the code contradicts this document, the code wins: say so in your
report rather than bending the code to match the doc.

- Pre-registered constants, verbatim: PAPER_BASE = "https://paper-api.alpaca.markets" (hardcoded, never live); RULES_VERSION = "v1-top10-equal-monthly"; TOP_N = 10; MAX_WEIGHT = 0.10; MAX_PANEL_AGE_DAYS = 45. Also LICENSE_NOTE = "Paper-account journal: private forward-evidence record; never redistribute."  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:36-41`)*
- The pre-registered selection is one pure function: target_portfolio(rows, equity) -> dict[str, float]. It filters rows where r.get('graded') and r.get('score') is not None, sorts by key=lambda r: (-float(r['score']), str(r['ticker'])), takes [:TOP_N], and returns {str(r['ticker']): round(min(equity*MAX_WEIGHT, equity/len(picks)), 2)}. It takes plain dict rows and a float — no broker object — so it is directly reusable by a simulator.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:164-173 (target_portfolio)`)*
- The rebalance band rule, verbatim: for each target, held = float(current.get(symbol, {}).get('market_value', 0.0)); delta = notional - held; `if abs(delta) < max(25.0, notional * 0.1): continue  # rebalance bands: skip trivial adjustments`. Action label is 'buy' when delta > 0 else 'trim'; the broker order side is 'buy'/'sell' on abs(delta).  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:244-260`)*
- Order sequencing in rebalance(): closes first over sorted(set(current) - set(targets)) (one action dict {'action':'close','symbol':sym}), THEN buys/trims over sorted(targets.items()) (action dict {'action':<buy|trim>,'symbol':sym,'notional':round(abs(delta),2)}). A per-symbol PaperTradingError is caught and appended to `failed` as {'action':..., 'symbol':..., 'error': str(exc)[:200]} so one bad symbol never aborts the loop.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:236-261`)*
- Watermark/idempotency design: journal_dir = Path(vault_dir)/'paper_journal'; watermark_path = journal_dir/'.last_panel.json' holding {'signal_date': <panel stem>}. If the stored signal_date equals the panel's, rebalance returns {'signal_date':..., 'orders':0, 'skipped':True} without trading. The watermark is written last, atomically (tmp .json.tmp then .replace), and is deliberately NOT advanced when every order failed so the next run retries the same panel.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:198-210, 275-286`)*
- Exact rebalance record shape: {'kind':'rebalance', 'rules_version':RULES_VERSION, 'signal_date':str, 'executed_utc':dt.datetime.now(dt.UTC).isoformat(), 'equity_before':float, 'targets':{panel_symbol: notional_float}, 'actions':[...]} plus optional 'failed':[...] only when non-empty. There is NO 'arm' key and NO 'date' key today.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:262-272`)*
- Exact snapshot record shape: {'kind':'snapshot', 'date':today.isoformat(), 'captured_utc':now(UTC).isoformat(), 'equity':float, 'cash':float, 'positions':[{'symbol','qty','market_value','avg_entry_price','unrealized_pl'}]}. equity/cash go through _account_value() and are floats; the five position fields are passed through UNCHANGED from the Alpaca payload, which serializes account numbers as strings (see the _account_value docstring). A real on-disk record confirms the top-level shape.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:296-310; C:/Users/tforstrom/Desktop/Stock-Vault/data/paper_journal/2026-07.jsonl.gz`)*
- Live journal contents today (2 records, both snapshots, equity 100000.0, cash 100000.0, positions []): {"captured_utc": "2026-07-30T12:56:07.564939+00:00", "cash": 100000.0, "date": "2026-07-30", "equity": 100000.0, "kind": "snapshot", "positions": []}. So the real account's starting equity is exactly 100000.0 and there are ZERO fill/benchmark/rebalance records on disk yet.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/paper_journal/2026-07.jsonl.gz`)*
- Journal writer: _append_journal(journal_dir, day, record) writes one gzipped JSONL per MONTH at journal_dir/f"{day.strftime('%Y-%m')}.jsonl.gz". It decompresses the existing file, appends json.dumps(record, sort_keys=True)+'\n', and rewrites via `with gzip.GzipFile(tmp, 'wb', mtime=0)` where tmp = path.with_suffix('.gz.tmp'), then tmp.replace(path).  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:320-331`)*
- VERIFIED EXPERIMENTALLY: gzip.GzipFile(<path str>, 'wb', mtime=0) embeds the tmp file's basename in the gzip FNAME header, so the output bytes depend on the temp filename. Writing the same payload via 'x.jsonl.gz.tmp' vs 'y.jsonl.gz.tmp' produced DIFFERENT bytes (headers b'\x1f\x8b\x08\x08\x00\x00\x00\x00\x02\xffx.jsonl.gz.tmp' vs '...y.jsonl.gz.tmp'). Using gzip.GzipFile(filename='', mode='wb', fileobj=<open file>, mtime=0) drops the FNAME flag entirely (header b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff'). Local zlib is 1.3.1.  
  *(`verified by running python against C:/Users/tforstrom/Desktop/Stock-Vault (gzip stdlib behavior)`)*
- Symbology helpers already exist and are exact: _to_broker_symbol(s) = s.upper().replace('-', '.') (panel dash form -> Alpaca/Polygon dot form) and _to_panel_symbol(s) = s.upper().replace('.', '-') (the canonical diff key). rebalance() applies _to_panel_symbol to targets and raises on post-normalization collisions.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:105-112, 224-231`)*
- load_frozen_panel(source) currently globs ONLY the flat layout: frozen_dir = Path(source)/'frozen_scores'; candidates = sorted(p for p in frozen_dir.glob('*.parquet') if _PANEL_STEM.fullmatch(p.stem)) where _PANEL_STEM = re.compile(r'^\d{4}-\d{2}-\d{2}$'); it takes candidates[-1] and requires columns {'ticker','score','graded'}. It raises PaperTradingError(f'no frozen panels under {frozen_dir}') when empty.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py:43,46,134-162`)*
- IN-FLIGHT AND ALREADY ON DISK: the grader has moved panels to frozen_scores/<profile>/YYYY-MM-DD.parquet. `ls -R` shows only frozen_scores/all_weather/2026-07-30.parquet, and `git status` in Stock-Grader shows ' D frozen_scores/2026-07-30.parquet' + '?? frozen_scores/all_weather/'. Therefore paper.load_frozen_panel's flat glob now finds ZERO files and the real trader is BROKEN until it is taught the per-profile layout.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/frozen_scores/ (directory listing + git status)`)*
- Frozen panel schema (13 columns, verified by reading the parquet): signal_date(str), ticker(str), cik(str), score(float64), letter(str), percentile(float64), coverage(float64), graded(bool), profile(str), config_fingerprint(str), universe_fingerprint(str), code_commit(str), schema_version(str='1.0'). The 2026-07-30 all_weather panel has 82 rows, 78 graded, profile column == ['all_weather']. Top-10 by (score desc, ticker asc): NVDA 99.36, META 98.08, ADBE 96.79, EOG 95.51, CMCSA 94.23, MSFT 92.95, MA 91.67, CRM 90.38, AXP 89.10, GOOGL 87.82.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/frozen_scores/all_weather/2026-07-30.parquet`)*
- There are exactly 11 profiles: ['all_weather','value','deep_value','growth','garp','quality','momentum','low_volatility','dividend_income','dividend_growth','turnaround'], available as profiles.profile_names() and profiles.PROFILE_SPECS (len 11).  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/profiles.py:33+ (PROFILE_SPECS), verified via python -c import`)*
- cmd_freeze (in-flight, uncommitted) now writes panel_path(profile) = out_dir/profile/f'{signal_date}.parquet', drives profiles = profile_names() when args.all_profiles else [args.profile], builds snapshots ONCE, and applies the min_letter_peers refusal PER PROFILE (a refused profile does not suppress the others). monthly-freeze.yml was changed to `stock-grader freeze --all-profiles --universe config/universe_default.txt --out frozen_scores`.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/cli.py:881-990 (working tree) and .github/workflows/monthly-freeze.yml (working tree)`)*
- market_eod on-disk layout: <vault_dir>/market_eod/YYYY-MM/YYYY-MM-DD.jsonl.gz, written by _day_path(). Each line is json.dumps(row, sort_keys=True) with EXACT keys: symbol, open, high, low, close, volume, vwap, transactions. Real sample line: {"close": 53.95, "high": 53.983, "low": 53.45, "open": 53.83, "symbol": "PRFZ", "transactions": 559, "volume": 56218.93163, "vwap": 53.813}. 2026-07-28 has 12482 rows.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/market_eod.py:62-75; data/market_eod/2026-07/2026-07-28.jsonl.gz`)*
- market_eod bars are UNADJUSTED: fetch_day passes params={'adjusted': 'false', 'include_otc': 'false', ...}. A split therefore appears as a raw close-to-close price jump with no share adjustment anywhere in the archive.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/market_eod.py:35-42`)*
- market_eod uses POLYGON DOT symbology, matching _to_broker_symbol. Verified on 2026-07-28: 'BRK.B' present (close 512.37), 'BRK-B' absent; 'MOG.A' present (close 414.56); 89 of 12482 symbols contain a dot. 'SPY' is present (close 740.86, vwap 739.9603) and 'VOO' (680.96), so a benchmark series needs no new collector.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/2026-07/2026-07-28.jsonl.gz`)*
- Every market_eod month directory already carries manifest.json (checked all 25 month dirs; none missing). data/market_eod/2026-07/manifest.json has schema_version '1.0', 19 file entries with sha256+bytes, license_note starting 'Massive (ex-Polygon) free-tier data: personal use; ...'. Archive spans 2024-07 through 2026-07 (25 month dirs).  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/data/market_eod/*/manifest.json`)*
- Manifest helper signature: write_manifest(dataset_dir: str, *, source_urls: list[str], license_note: str, extra: dict[str,object] | None = None) -> dict. SCHEMA_VERSION = '1.0'. It enumerates sorted(os.listdir(dataset_dir)) and SKIPS 'manifest.json', any name starting with '.', and non-files — so dot-prefixed state files are invisible to it. It sets generated_at_utc from dt.datetime.now(dt.UTC) (non-deterministic) and merges `extra` at the TOP LEVEL via manifest.update(extra). Writes via atomic_write_text with json.dumps(indent=2, sort_keys=True).  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/manifest.py:12,23-51`)*
- Staleness module: _CHECKS = {'borrow','market-eod','recs','finra','paper'} and DATASETS = tuple(_CHECKS). check_paper(vault_dir, now) reads <vault>/paper_journal, matches _PAPER_NAME = ^(\d{4}-\d{2})\.jsonl\.gz$, then derives the clock from manifest.json's generated_at_utc against PAPER_MAX_AGE = timedelta(days=6). check_datasets() collects all failures into one StalenessError. Useful reusable helpers already exported: previous_market_day(), is_market_holiday(), _parse_manifest_timestamp(), _latest_filename_value().  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/staleness.py:26-27,242-262,263-270`)*
- Vault CLI structure: argparse with a `shared` parent parser carrying --vault-dir (default os.environ.get('STOCK_VAULT_DIR','data')), and every subcommand created with parents=[shared]. Existing subcommands: harvest-delisted, market-eod, borrow, ssga, paper-rebalance (--panel-source required), paper-journal, recs, finra-short-interest, check-staleness. Entry point stock-vault = stock_vault.cli:main.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/cli.py:39-110`)*
- The vault package has NO reader for its own market_eod archive — market_eod.py only writes. The only existing reader lives in the GRADER (VaultDataSource.market_eod_day/market_eod_available_days/market_eod_series), which the vault must NOT import (ECOSYSTEM rule 1: no repo imports another repo's code). Its manifest discipline is the pattern to copy: SUPPORTED_SCHEMA_VERSIONS = frozenset({'1.0'}), refuse files not listed in manifest.json, sha256-verify before trusting a row.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/src/stock_grader/data/vault.py:36,55-89,93-112`)*
- Deterministic gzip is already the house style elsewhere: market_eod._write_day and paper._append_journal both use gzip.GzipFile(..., mtime=0) and json.dumps(..., sort_keys=True), and .gitattributes exists in the vault. Vault pyproject has optional-dependencies dev = [pytest, ruff, pandas>=2.0, pyarrow>=14] and paper = [pandas>=2.0, pyarrow>=14]; ruff line-length 100, lint select E,F,W,I,UP,B; pytest testpaths=['tests'].  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/pyproject.toml; src/stock_vault/market_eod.py:70-75`)*
- Stock-Data corporate_actions is NOT usable for split adjustment: data/corporate_actions/splits.jsonl contains exactly 2 lines, both AAPL (2014-06-06 ratio 7.0, 2020-08-28 ratio 4.0), and the manifest's tickers_requested is only ['AAPL','JNJ','O'] with basis 'current_fully_split_adjusted'. dividends.parquet has 448 rows over the same 3 tickers.  
  *(`C:/Users/tforstrom/Desktop/Stock-Data/data/corporate_actions/splits.jsonl and manifest.json`)*
- Frozen score panels carry NO manifest.json — frozen_scores/ and frozen_scores/all_weather/ contain only .parquet files, and the panel is tracked in git (git ls-files shows frozen_scores/2026-07-30.parquet). The only in-band contract available to a consumer is the panel's own schema_version column (value '1.0') plus config_fingerprint / universe_fingerprint / code_commit columns.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/frozen_scores/ (listing + git ls-files)`)*
- The vault's existing test fixtures show the exact stub/fixture idiom to follow: tests/test_paper.py defines class StubAlpaca (clock/account/positions/close_position/submit_notional_order), build_panel(root, signal_date, scores) writing frozen_scores/<date>.parquet via pandas, and read_journal(vault) decompressing paper_journal/2026-07.jsonl.gz into dicts. TODAY = dt.date(2026,7,30).  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/tests/test_paper.py:23-77`)*
- Vault workflows and their clocks: collectors.yml (cron '23 22 * * 1-5' after US close, plus '41 14 * * 1-5') runs check-staleness --pre-collection then market-eod; paper-trader.yml (cron '37 15 * * 1-5') checks out TylerJForstrom/Stock-Grader into path 'grader', runs `pip install -e .[paper]`, then paper-rebalance --panel-source grader and paper-journal. Both commit with a `for attempt in 1 2 3; do git push ... git pull --rebase` retry loop. Market EOD for day D lands at 22:23 UTC on D+1, i.e. AFTER paper-trader.yml's 15:37 slot.  
  *(`C:/Users/tforstrom/Desktop/Stock-Vault/.github/workflows/collectors.yml, .github/workflows/paper-trader.yml`)*
- HANDOFF working rules that bind this change: (2) never wrap a documented JSON schema in an envelope — add keys additively (this mistake was made twice this week); (3) CLI args that workflows pass subcommand-first MUST be registered on the subparser via the parents=[shared] pattern (the 'DOA workflow' bug class); (5) after dispatching any workflow, VERIFY the run completed with gh run list. Item 10 already lists 'Paper-trade bridge ... consuming frozen_scores (artifacts-not-imports)' as deferred.  
  *(`C:/Users/tforstrom/Desktop/Stock-Grader/docs/HANDOFF.md (Working rules 2,3,5; queue item 10)`)*

## Implementation steps

### Step 1. Teach the panel loader the per-profile layout (UNBLOCKS THE REAL TRADER TOO)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py`, `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/cli.py`

In paper.py, change `load_frozen_panel(source: str, profile: str = "all_weather")` to resolve panels in this order and add a sibling enumerator used by the simulator:

  def _panel_dir(source, profile) -> tuple[Path, bool]:  # (dir, is_per_profile)
      root = Path(source) / "frozen_scores"
      per = root / profile
      if per.is_dir(): return per, True
      return root, False   # legacy flat layout

  def frozen_panel_dates(source, profile) -> list[tuple[str, Path]]:
      '''Every panel for one profile, oldest first. Stems must match _PANEL_STEM.'''

Rules: (a) in the per-profile dir, accept every YYYY-MM-DD.parquet; (b) in the legacy FLAT dir, read each parquet and keep it only if its `profile` column's unique value equals the requested profile (the column exists and is populated — verified). Keep the existing `_REQUIRED_PANEL_COLUMNS = {'ticker','score','graded'}` check, and ADD a schema gate: the panel's `schema_version` column, when present, must be in `SUPPORTED_PANEL_SCHEMA_VERSIONS = frozenset({'1.0'})`, else raise PaperTradingError — this is the only artifact-contract handle a panel offers (it has no manifest.json).

`load_frozen_panel` keeps returning (signal_date, rows) for the NEWEST panel and keeps raising PaperTradingError when there are none. Update cli.py's paper-rebalance path to pass `profile="all_weather"` explicitly (add `--profile` to the p_reb subparser with default 'all_weather', registered via parents=[shared] style on the SUBPARSER — HANDOFF rule 3).

COORDINATION: paper.py may be edited concurrently. Before editing, run `git -C <vault> diff --stat src/stock_vault/paper.py`; if dirty, rebase your edit onto whatever is there rather than reverting it. Do NOT change any pre-registered constant or the body of target_portfolio.

### Step 2. Extract a byte-stable append-only journal writer into stock_vault/journal.py

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/journal.py`, `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/paper.py`

New module `src/stock_vault/journal.py`:

  import gzip, hashlib, json, os
  from pathlib import Path

  def encode_records(records: list[dict]) -> bytes:
      return b''.join((json.dumps(r, sort_keys=True) + '\n').encode('utf-8') for r in records)

  def write_gzip_atomic(path: Path, payload: bytes) -> None:
      '''Byte-stable gzip: no FNAME, no mtime, fixed compresslevel.'''
      tmp = path.with_name(path.name + '.tmp')
      with open(tmp, 'wb') as fh:
          with gzip.GzipFile(filename='', mode='wb', fileobj=fh, mtime=0, compresslevel=9) as gz:
              gz.write(payload)
      os.replace(tmp, path)

  def read_records(path: Path) -> list[dict]:  # gzip.decompress + json.loads per non-blank line
  def append_records(month_path: Path, records: list[dict]) -> None:  # decompress existing, concat, write_gzip_atomic
  def payload_sha256(path: Path) -> str:  # sha256 of the DECOMPRESSED payload

WHY filename='' + fileobj: VERIFIED that gzip.GzipFile(<path>, 'wb', mtime=0) embeds the temp file's basename in the FNAME header, so the current paper.py writer's bytes depend on the tmp filename. The fileobj form emits a header with the FNAME flag clear.

Then refactor paper._append_journal to delegate to journal.append_records (pure implementation change; the record schema and the monthly f'{day:%Y-%m}.jsonl.gz' filename are unchanged). Note this rewrites existing paper_journal bytes on the next append — that is fine and intended (content is preserved; only the gzip header changes). Add a comment saying so.

### Step 3. Build a hash-verified market_eod reader inside the vault (no cross-repo import)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/eod.py`

New module `src/stock_vault/eod.py`. Mirror the grader's VaultDataSource discipline but stdlib-only (no pandas — 12k rows/day x ~500 days):

  SUPPORTED_SCHEMA_VERSIONS = frozenset({'1.0'})
  class EodError(RuntimeError): ...
  class MarketEod:
      def __init__(self, vault_dir: str | Path, *, verify_hashes: bool = True)
          # root = Path(vault_dir) / 'market_eod'
      def available_days(self) -> list[dt.date]
          # sorted; scan root/YYYY-MM/*.jsonl.gz, parse the stem with dt.date.fromisoformat, skip unparseable
      def closes(self, day: dt.date) -> dict[str, float]
          # manifest-gated: load root/<YYYY-MM>/manifest.json, refuse schema_version not in SUPPORTED,
          # refuse a file not listed in manifest['files'], sha256-verify the raw bytes against the entry,
          # then gzip.decompress + json.loads each line -> {row['symbol'].upper(): float(row['close'])}
          # drop rows whose close is None or non-finite. Cache per day in an LRU dict of bounded size (<=8).
      def next_session(self, after: dt.date) -> dt.date | None   # min available day strictly greater than `after`

Determinism: available_days() must be sorted and derived only from filenames. Performance: a full 11-arm rebuild parses each day file exactly once because the driver in step 4 is SESSION-MAJOR (outer loop sessions, inner loop arms) — do not invert it.

### Step 4. Write the simulator: stock_vault/shadow.py

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/shadow.py`

Constants at module top (these ARE the version stamp):

  from .paper import (MAX_PANEL_AGE_DAYS, RULES_VERSION, TOP_N, MAX_WEIGHT,
                      target_portfolio, _to_broker_symbol, _to_panel_symbol,
                      frozen_panel_dates)
  SIM_VERSION = 'sim-v1-nextclose-5bps'   # bump on ANY behavioral change, incl. cost
  COST_BPS = 5.0
  START_EQUITY = 100_000.0                # matches the real account's observed 100000.0
  BENCHMARK_SYMBOL = 'SPY'                # present in the archive; verified
  STALE_MARK_MAX_SESSIONS = 10
  SPLIT_CANDIDATES = (2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 1.5, 2/3, 0.5, 0.1)
  SPLIT_TOLERANCE = 0.02
  LICENSE_NOTE = ('Shadow paper arms: derived from Massive (ex-Polygon) free-tier EOD bars and '
                  'private frozen score panels; private forward-evidence record; never redistribute.')
  class ShadowError(RuntimeError): ...

ARM IDENTITY AND VERSION STAMPING (this is what keeps old arms interpretable):
  arm_name(profile) -> f'shadow:{profile}:{RULES_VERSION}+{SIM_VERSION}'
  arm_dir(vault_dir, profile) -> Path(vault_dir)/'shadow_journal'/profile/f'{RULES_VERSION}__{SIM_VERSION}'
A rules or cost change bumps RULES_VERSION or SIM_VERSION, which changes the DIRECTORY, which starts a NEW arm from the panels available at that moment. Old arm directories are never touched again and stay fully interpretable from their own arm.json. On first run write arm.json (manifested, no leading dot):
  {'schema_version':'1.0','arm':<arm_name>,'profile':p,'rules_version':RULES_VERSION,
   'sim_version':SIM_VERSION,'cost_bps':COST_BPS,'start_equity':START_EQUITY,
   'top_n':TOP_N,'max_weight':MAX_WEIGHT,'max_panel_age_days':MAX_PANEL_AGE_DAYS,
   'benchmark_symbol':BENCHMARK_SYMBOL,'first_signal_date':<oldest panel date>}
On every later run, compare EVERY key against the live constants; any mismatch raises ShadowError telling the operator to bump SIM_VERSION. This is the fail-closed guard against silently editing COST_BPS in place.

STATE (dot-prefixed so write_manifest skips it — verified behavior): arm_dir/'.state.json' =
  {'last_session':'YYYY-MM-DD'|None,'last_signal_date':'YYYY-MM-DD'|None,'cash':float,
   'positions':{panel_symbol: shares_float},'last_close':{panel_symbol: float},
   'stale_sessions':{panel_symbol:int},'journal_sha256':{'YYYY-MM.jsonl.gz': <payload sha256>}}
On load, re-verify each listed journal file's payload_sha256; a mismatch raises ShadowError('journal <name> changed under the simulator; rerun with --rebuild'). --rebuild ignores and deletes state + every *.jsonl.gz in the arm dir and replays from scratch.

DRIVER: run(vault_dir, panel_source, *, profiles=None, rebuild=False, log=print) -> dict
  profiles default = every subdirectory name under <panel_source>/frozen_scores (fall back to reading distinct `profile` values from flat panels). Build per-arm context: panels = frozen_panel_dates(panel_source, p) -> [(signal_date, path)]; skip a profile with no panels (log, no directory created).
  sessions = eod.available_days(); for each arm compute first_fill = eod.next_session(date(first_signal_date)); iterate D over sessions SESSION-MAJOR, and for each arm where D >= first_fill and (state.last_session is None or D > state.last_session), do a session step. Buffer records per (arm, month) and flush with journal.append_records at the end of the run.

SESSION STEP for arm A at session D (record emission order is fixed: corporate_action, interlock|rebalance, fills, snapshot, benchmark):
  1. closes = eod.closes(D).
  2. SPLIT GUARD over sorted(state.positions): prev = state.last_close.get(sym); cur = closes.get(_to_broker_symbol(sym)); if both present and ratio = prev/cur falls outside [0.55, 1.85], match ratio against SPLIT_CANDIDATES within SPLIT_TOLERANCE relative error. On a match, multiply the held share count by that ratio and emit {'kind':'corporate_action', ..., 'applied':True, 'matched':'<n>:1'}; on no match emit the same record with 'applied':False and leave shares alone. Document the limitation: market_eod is UNADJUSTED (adjusted=false) and Stock-Data's splits.jsonl covers only 3 tickers, so this heuristic is the honest best available; leave a TODO pointing at Stock-Data corporate_actions.
  3. REBALANCE DUE? S = the newest panel signal_date such that eod.next_session(S) == D. If S exists and S != state.last_signal_date:
       age = (D - date(S)).days. If age > MAX_PANEL_AGE_DAYS: emit {'kind':'interlock','reason':'panel_age_exceeds_max','detail':...} and do NOT advance last_signal_date (mirrors the real trader holding its watermark). Else run the rebalance below and set last_signal_date = S.
  4. REBALANCE (identical rules, simulated execution):
       mark(sym) = closes.get(_to_broker_symbol(sym)) or state.last_close[sym] (carry-forward)
       equity = cash + sum(shares * mark(sym))
       raw = target_portfolio(rows_from_panel_parquet, equity)      # CALL IT, never reimplement
       targets = {}; for t,n in raw.items(): k=_to_panel_symbol(t); if k in targets: raise ShadowError(collision); targets[k]=n
       if not targets: emit interlock reason 'no_graded_rows'; return (never liquidate — mirrors paper.py)
       CLOSES FIRST over sorted(set(positions) - set(targets)): px = closes.get(broker(sym)); if px is None -> failed.append({'action':'close','symbol':sym,'error':f'no archived close on {D}'}) and keep going; else fill_px = px*(1 - COST_BPS/1e4), qty = positions.pop(sym), cash += qty*fill_px, actions.append({'action':'close','symbol':sym}), emit a fill.
       THEN targets over sorted(targets.items()): px = closes.get(broker(sym)); None -> failed and continue.
         held_value = positions.get(sym,0.0)*px; delta = notional - held_value
         if abs(delta) < max(25.0, notional*0.1): continue        # EXACT band from paper.py:248
         if delta > 0: spend = min(delta, cash); if spend < 25.0 -> failed.append({'action':'buy','symbol':sym,'error':'insufficient simulated cash (no margin)'}) and continue; fill_px = px*(1+COST_BPS/1e4); qty = spend/fill_px; positions[sym]+=qty; cash -= spend; actions.append({'action':'buy','symbol':sym,'notional':round(spend,2)})
         else: proceeds = min(-delta, positions.get(sym,0.0)*px); fill_px = px*(1-COST_BPS/1e4); qty = proceeds/fill_px; positions[sym] -= qty; drop the key when it falls below 1e-9; cash += proceeds; actions.append({'action':'trim','symbol':sym,'notional':round(proceeds,2)})
         emit a fill for each executed leg.
       Stated modeling deviations, put in the module docstring: (a) the real arm sizes on the broker's intraday `equity` while the sim sizes on D's close; (b) the sim never borrows — an over-budget buy becomes a `failed` leg exactly as a broker rejection would.
  5. MARK + SNAPSHOT: for each held sym use closes when present (reset stale_sessions[sym]=0, update last_close) else carry last_close and increment stale_sessions[sym]; when stale_sessions[sym] > STALE_MARK_MAX_SESSIONS, mark the position to 0.0 and drop it (delisting write-down), listing it in the snapshot's 'stale_marks'. Emit the snapshot.
  6. BENCHMARK: if BENCHMARK_SYMBOL in closes, emit the benchmark record. It is duplicated into every arm's journal on purpose so each arm file is self-contained and an excess-of-SPY curve needs exactly one file.

EXACT RECORD SHAPES (every shadow record carries arm/rules_version/sim_version/simulated; wall-clock fields are present-but-null so ONE reader serves both arms and nothing pretends to be an observation):
  rebalance: {'kind':'rebalance','arm':A,'rules_version':RV,'sim_version':SV,'simulated':true,
              'signal_date':S,'date':D,'executed_utc':null,'equity_before':round(equity,2),
              'targets':{sym:notional},'actions':[...]}  + 'failed':[...] only when non-empty
  fill:      {'kind':'fill','arm':A,'rules_version':RV,'sim_version':SV,'simulated':true,
              'signal_date':S,'date':D,'symbol':<panel dash form>,'side':'buy'|'sell',
              'qty':round(qty,6),'price':round(fill_px,4),'notional':round(notional,2),
              'reference_close':round(px,4),'cost_bps':COST_BPS,'filled_at_utc':null}
  snapshot:  {'kind':'snapshot','arm':A,'rules_version':RV,'sim_version':SV,'simulated':true,
              'date':D,'captured_utc':null,'equity':round(equity,2),'cash':round(cash,2),
              'positions':[{'symbol':sym,'qty':f'{q:.6f}','market_value':f'{mv:.2f}',
                            'avg_entry_price':f'{avg:.4f}','unrealized_pl':f'{pl:.2f}'}, ...]}
              + 'stale_marks':[...] only when non-empty
              NOTE the position field values are STRINGS: the real journal passes Alpaca's payload through
              unchanged and Alpaca serializes account numbers as strings, so matching types is what makes
              one reader work. equity/cash are floats in both arms (the real ones go through _account_value).
              Track a per-symbol cost basis in state to fill avg_entry_price/unrealized_pl.
  benchmark: {'kind':'benchmark','arm':A,'sim_version':SV,'simulated':true,'date':D,
              'symbol':'SPY','close':round(px,4),'source':'market_eod'}
  interlock: {'kind':'interlock','arm':A,'rules_version':RV,'sim_version':SV,'simulated':true,
              'date':D,'signal_date':S,'reason':str,'detail':str}
  corporate_action: {'kind':'corporate_action','arm':A,'sim_version':SV,'simulated':true,'date':D,
              'symbol':sym,'prev_close':float,'close':float,'ratio':round(ratio,6),
              'matched':'4:1'|null,'applied':bool}

RECONCILE WITH THE IN-FLIGHT REAL RECORDS: paper.py is gaining {'kind':'benchmark'} and {'kind':'fill'}. Before finalizing, run `git -C <vault> log -p -- src/stock_vault/paper.py | grep -n '"kind"'` and re-read paper.py. If the real fill/benchmark records use different key NAMES for the same concepts, adopt the REAL names in the shadow and update this spec's shapes — one reader beats a prettier schema. Whatever happens, do not rename or wrap any EXISTING key (HANDOFF rule 2: additive only).

MANIFEST: after writing an arm's journals call write_manifest(str(arm_dir), source_urls=[GROUPED_URL_TEMPLATE, 'local:Stock-Grader/frozen_scores'], license_note=LICENSE_NOTE, extra={'arm':A,'profile':p,'rules_version':RV,'sim_version':SV,'cost_bps':COST_BPS,'payload_sha256':{name: sha}}). generated_at_utc is now()-based and therefore the ONE non-deterministic artifact in the arm dir — the determinism test must compare *.jsonl.gz only.

NO WALL CLOCK ANYWHERE: shadow.py must not import or call datetime.now / date.today / time.time / random. Every date comes from a panel stem or an archive filename. Enforce with a test (step 9).

### Step 5. One reader for both arms: stock_vault/arms.py

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/arms.py`

New module `src/stock_vault/arms.py`:

  @dataclass(frozen=True)
  class ArmRef: arm: str; profile: str | None; directory: Path; simulated: bool

  def list_arms(vault_dir) -> list[ArmRef]
      # 'alpaca_paper' (profile 'all_weather', simulated False) when <vault>/paper_journal exists,
      # plus every <vault>/shadow_journal/<profile>/<rules>__<sim>/ containing arm.json. Sorted by arm name.
  def load_arm(vault_dir, arm: str) -> list[dict]
      # concatenate every YYYY-MM.jsonl.gz in the arm dir in filename order, then STABLE-sort by
      # (record.get('date') or record.get('signal_date') or '', KIND_ORDER[kind], index)
      # KIND_ORDER = {'corporate_action':0,'interlock':1,'rebalance':2,'fill':3,'snapshot':4,'benchmark':5}
      # BACKWARD COMPAT: a record with no 'arm' key belongs to 'alpaca_paper' (the real journal has no
      # 'arm' key today and must keep working untouched).
  def equity_curve(records) -> list[tuple[dt.date, float]]   # from kind=='snapshot', float(r['equity'])
  def benchmark_curve(records) -> list[tuple[dt.date, float]]
  def fills(records) -> list[dict]
  def _num(value) -> float   # tolerate str|float — real position fields are strings, equity is a float

OPTIONAL, ADDITIVE, DO LAST (paper.py is in flux): add "arm": "alpaca_paper" to the real rebalance and snapshot records. Purely additive, allowed by HANDOFF rule 2. The reader must work with OR without it — write the reader first, test the missing-arm default, then decide.

### Step 6. Calibrate the simulator's fill assumption against the real account

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/shadow.py`

In shadow.py add:

  def calibrate(vault_dir, *, profile='all_weather', log=print) -> dict

The control twin matters: run the sim over ALL 11 profiles INCLUDING all_weather, because all_weather is the profile the real Alpaca account trades (monthly-freeze.yml froze --profile all_weather; the workflow now freezes all profiles and paper-rebalance stays on all_weather). shadow:all_weather is therefore an apples-to-apples twin of the real arm; the other ten are the new evidence.

Algorithm: real = [r for r in arms.load_arm(vault,'alpaca_paper') if r['kind']=='fill']; sim = fills of the newest shadow all_weather arm. Join on the key (signal_date, symbol, side). For each pair take reference_close from the sim record (or, if the real record carries one, prefer the real record's), and compute
    realized_bps = 1e4 * (real_price/reference_close - 1) * (1 if side=='buy' else -1)
    modeled_bps  = COST_BPS
Report {'pairs':n,'cost_bps':COST_BPS,'realized_bps_median':...,'realized_bps_mean':...,'realized_bps_p90':...,'per_symbol':[...],'suggested_cost_bps':median,'note':...} printed as json.dumps(..., indent=2, sort_keys=True) to stdout. Write NOTHING to the archive — the report is derived and rerunnable, and writing it would put a non-deterministic file inside a manifested dir.

HARD RULE, state it in the docstring: calibrate NEVER mutates COST_BPS. Adopting a new cost is a code commit that edits COST_BPS *and* bumps SIM_VERSION, which starts fresh arm directories and leaves every existing arm byte-frozen. Silent retuning would rewrite history and destroy the pre-registration.

Today there are ZERO real fill records on disk, so calibrate must return {'pairs': 0, 'note': 'no {"kind":"fill"} records in the real journal yet; the real trader has only journaled snapshots'} and exit 0 rather than raising.

### Step 7. CLI subcommands (register EVERY arg on the subparser)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/cli.py`

In cli.py, following the existing parents=[shared] pattern exactly (HANDOFF rule 3 — top-level-only args silently killed scheduled runs in two repos):

  p_shadow = sub.add_parser('shadow-run', parents=[shared], help='replay the pre-registered rules over every profile panel into per-arm shadow journals')
  p_shadow.add_argument('--panel-source', required=True, help='local Stock-Grader clone containing frozen_scores/')
  p_shadow.add_argument('--profiles', nargs='*', default=None, help='default: every profile with panels')
  p_shadow.add_argument('--rebuild', action='store_true', help='discard state and replay from the first panel (must reproduce identical journal bytes)')

  p_arms = sub.add_parser('arms', parents=[shared], help='list every forward-evidence arm (real + shadow) with its last session and equity')
  p_arms.add_argument('--format', choices=('table','json'), default='table')

  p_cal = sub.add_parser('shadow-calibrate', parents=[shared], help='compare real Alpaca fills against the simulator on the same profile')
  p_cal.add_argument('--profile', default='all_weather')

Dispatch in main(): shadow-run -> shadow.run(...) printing the returned stats; arms -> arms.list_arms + equity_curve tail; shadow-calibrate -> print json. Return 1 (not an exception) when shadow.run reports any arm with failed legs, matching the paper-rebalance convention at cli.py's paper branch. Also add 'shadow' to staleness DATASETS (step 8) so check-staleness accepts it.

### Step 8. Shadow clock in staleness.py + the scheduled workflow

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/src/stock_vault/staleness.py`, `C:/Users/tforstrom/Desktop/Stock-Vault/.github/workflows/shadow-arms.yml`, `C:/Users/tforstrom/Desktop/Stock-Vault/.github/workflows/collectors.yml`

staleness.py: add SHADOW_MAX_AGE = dt.timedelta(days=6) (same weekday cadence + slack rationale as PAPER_MAX_AGE) and

  def check_shadow(vault_dir, now=None) -> str:
      '''Newest shadow-arm manifest write within six days.'''
      # walk <vault>/shadow_journal/*/*/manifest.json, take the max _parse_manifest_timestamp(generated_at_utc),
      # raise StalenessError('shadow: no arm manifests found') when none, else compare against SHADOW_MAX_AGE

Register it in _CHECKS (DATASETS derives from it automatically) as key 'shadow'.

New workflow `.github/workflows/shadow-arms.yml`. It must run AFTER market EOD lands: collectors.yml writes day D-1 at 22:23 UTC, and paper-trader.yml's 15:37 UTC slot is too early. Use cron '11 23 * * 1-5', workflow_dispatch, permissions: contents: write, concurrency group 'shadow-arms'. Steps: checkout; second actions/checkout@v4 with repository TylerJForstrom/Stock-Grader path 'grader' (copy paper-trader.yml verbatim); setup-python 3.12; `pip install -e .[paper]`; bootstrap-guarded staleness gate (`if [ -d data/shadow_journal ]; then stock-vault check-staleness --vault-dir data shadow; else echo 'bootstrap: no shadow arms yet'; fi`); `stock-vault shadow-run --vault-dir data --panel-source grader`; then the SAME commit block as the other vault workflows (git config bot identity, `git add -A data`, `git diff --cached --quiet` early exit, `for attempt in 1 2 3; do git push ... git pull --rebase origin main` retry). timeout-minutes: 60 (a cold full rebuild parses ~500 x 12k EOD rows). Add `- name: Verify shadow clock` and the shadow entry to collectors.yml's cross-coverage gate line as well, bootstrap-guarded the same way the paper clock already is.

After pushing, VERIFY with `gh run list --repo TylerJForstrom/Stock-Vault --limit 5` (HANDOFF rule 5 — a dispatched-but-dead run burned a day of PIT archive once).

### Step 9. Tests: tests/test_shadow.py (+ additions to test_paper.py, test_ops_hardening.py)

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/tests/test_shadow.py`, `C:/Users/tforstrom/Desktop/Stock-Vault/tests/test_paper.py`, `C:/Users/tforstrom/Desktop/Stock-Vault/tests/test_ops_hardening.py`

Follow tests/test_paper.py's idiom: a build_panel-style helper writing frozen_scores/<profile>/<date>.parquet via pandas, and a build_eod(tmp_path, {date: {symbol: close}}) helper writing data/market_eod/<YYYY-MM>/<date>.jsonl.gz with sorted-key JSON lines AND a matching manifest.json produced by manifest.write_manifest (so the reader's sha256 gate passes). Required test functions:

  test_shadow_calls_the_pre_registered_selection_function  — monkeypatch stock_vault.shadow.target_portfolio with a counting wrapper; assert it was called and that the chosen names equal paper.target_portfolio's output on the same rows/equity. This is the guarantee that the arms share rules.
  test_fill_uses_the_next_archived_session_close_with_cost_bps — panel 2026-07-30, archive has 07-30 and 07-31; assert fills are dated 07-31 and price == round(close*(1+COST_BPS/1e4), 4).
  test_rerun_is_byte_identical_to_a_full_rebuild — run incrementally session by session, hash every *.jsonl.gz, then run with --rebuild into a second tree and assert identical sha256 per file (compare compressed bytes AND journal.payload_sha256; ignore manifest.json, whose generated_at_utc is now()-based).
  test_second_run_with_no_new_sessions_writes_nothing — file mtimes/bytes unchanged, stats report 0 sessions.
  test_gzip_bytes_do_not_depend_on_the_temp_filename — write the same payload through journal.write_gzip_atomic to two differently named paths; assert the gzip bytes after the header's first 10 fixed fields are equal and that FLG&0x08 (FNAME) is clear. Regression for the verified stdlib behavior.
  test_no_wall_clock_in_shadow — ast.parse src/stock_vault/shadow.py and assert no attribute access named 'now', 'today', 'time', or a 'random' import; and assert every emitted record has executed_utc/captured_utc/filled_at_utc set to None where the key exists.
  test_arm_dir_is_version_stamped — arm dir name == f'{RULES_VERSION}__{SIM_VERSION}'; monkeypatch shadow.COST_BPS to 7.0 without changing SIM_VERSION and assert ShadowError mentioning 'bump SIM_VERSION'.
  test_records_share_the_real_journal_shapes — assert the shadow rebalance record's key set is a SUPERSET of the real one's ({'kind','rules_version','signal_date','executed_utc','equity_before','targets','actions'}) and the snapshot's is a superset of {'kind','date','captured_utc','equity','cash','positions'}; assert position field values are str and equity/cash are float.
  test_reader_serves_both_arms_and_defaults_missing_arm — arms.load_arm on a real paper_journal whose records have no 'arm' key returns them under 'alpaca_paper'; list_arms sees both.
  test_band_rule_skips_trivial_adjustments — a held position within max(25.0, notional*0.1) of target emits no fill.
  test_insufficient_cash_is_a_failed_leg_not_margin — assert cash never goes negative and the leg lands in record['failed'].
  test_stale_panel_trips_interlock_without_advancing_watermark — force an archive gap > MAX_PANEL_AGE_DAYS; assert an interlock record and that state['last_signal_date'] is unchanged.
  test_split_ratio_adjusts_shares_and_journals_a_corporate_action — 4:1 synthetic (close 400 -> 100); assert shares x4, a corporate_action record with matched '4:1' and applied True, and equity continuity within 1e-6.
  test_unmatched_price_jump_is_flagged_not_applied — a 0.4x jump matching no candidate leaves shares alone with applied False.
  test_delisted_holding_carries_last_close_then_writes_down — after STALE_MARK_MAX_SESSIONS the position is dropped and listed in stale_marks.
  test_class_share_symbology_maps_panel_dash_to_archive_dot — a BRK-B panel row fills against the archive's 'BRK.B' row.
  test_calibrate_reports_zero_pairs_without_real_fills — returns {'pairs': 0, ...} and does not raise.
  test_market_eod_reader_refuses_unlisted_or_tampered_file — flip a byte, expect EodError; drop the manifest entry, expect EodError; set schema_version '2.0', expect EodError.

Add to test_paper.py: test_per_profile_panel_layout_is_found and test_legacy_flat_panel_is_matched_by_profile_column. Add to test_ops_hardening.py: test_shadow_clock_uses_arm_manifest_timestamp and test_shadow_missing_arms_is_loud, mirroring the existing check_paper tests.

### Step 10. Docs and the ecosystem contract

*Files:* `C:/Users/tforstrom/Desktop/Stock-Vault/README.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/HANDOFF.md`, `C:/Users/tforstrom/Desktop/Stock-Grader/docs/REVIEW_FEEDBACK.md`, `C:/Users/tforstrom/Desktop/Stock-Data/ECOSYSTEM.md`

Stock-Vault README.md: add a collector-table row for `shadow-arms` next to the existing `paper-trader` row, in the same voice — the arm directory convention data/shadow_journal/<profile>/<rules_version>__<sim_version>/YYYY-MM.jsonl.gz, the cost model (COST_BPS, next-session-close fills), the determinism promise (a --rebuild reproduces identical journal bytes), and that a rules/cost change starts a NEW arm rather than rewriting an old one. State plainly that shadow arms are SIMULATED and every record carries 'simulated': true.

Stock-Grader docs/HANDOFF.md: under queue item 10, replace the 'Paper-trade bridge' deferred line with a DONE line naming the shadow arms, and add one line to docs/REVIEW_FEEDBACK.md's Agent log with the commit hash (AGENTS.md requires this).

Stock-Data ECOSYSTEM.md: one line in the Rules or sequencing section noting that shadow arms consume grader panels + vault EOD and write vault journals — same one-direction DAG (sources -> foundry -> grader -> forward), no new cycle, and no code import in either direction. Also record the gap this surfaced: frozen_scores/ panels carry NO manifest.json, so consumers currently gate on the panel's own schema_version column; a proper per-profile manifest is follow-up work for the grader, not this change.

Commit discipline: one coherent chunk per commit (steps 1-2, then 3-4, then 5-7, then 8-10), full `pytest -q` green before AND after each.

## Pitfalls

- THE REAL TRADER IS ALREADY BROKEN BY THE IN-FLIGHT FREEZE MOVE. paper.load_frozen_panel globs frozen_scores/*.parquet (paper.py:148), but the grader working tree has already moved the only panel to frozen_scores/all_weather/2026-07-30.parquet (git status shows ' D frozen_scores/2026-07-30.parquet' + '?? frozen_scores/all_weather/'). The next paper-trader.yml run will raise PaperTradingError('no frozen panels under .../frozen_scores'). Step 1 is not optional cleanup — it is the fix, and it must land before or with everything else.
- gzip.GzipFile(<path string>, 'wb', mtime=0) writes the TEMP FILE'S BASENAME into the gzip FNAME header (verified: identical payloads through 'x.jsonl.gz.tmp' and 'y.jsonl.gz.tmp' produced different bytes). Any byte-determinism claim built on the current paper._append_journal is false. Always write through gzip.GzipFile(filename='', mode='wb', fileobj=<open file>, mtime=0, compresslevel=9).
- write_manifest sets generated_at_utc from dt.datetime.now(dt.UTC) (manifest.py:38), so manifest.json is NON-deterministic by construction. The determinism test must compare *.jsonl.gz only. Conversely, that same timestamp is what check_shadow's clock reads, so do not try to make it deterministic.
- Compressed gzip bytes can differ across zlib builds even at the same compresslevel. Anchor the portable determinism claim on the sha256 of the DECOMPRESSED payload (journal.payload_sha256, also stored in the manifest's extra); compare compressed bytes only within a single machine/run.
- market_eod is UNADJUSTED (fetch_day passes adjusted='false'). Holding share counts across a split produces a fake -75% month. Stock-Data cannot rescue this: splits.jsonl has exactly 2 rows, both AAPL, and tickers_requested is only ['AAPL','JNJ','O']. The ratio-matching guard is a heuristic — it must journal what it did (applied True/False, matched ratio) so a future corporate-actions dataset can correct the record instead of silently disagreeing with it.
- PIT LEAKAGE: the fill session must be strictly AFTER the panel's signal_date and must come from the archive's own available_days, never from a calendar. Filling at the signal_date's own close would use a price that did not exist when the panel was frozen, and the whole point of the frozen panel is that it predates the future. Also never re-read a panel file after its date — panels are immutable and a re-freeze would silently rewrite history.
- Do NOT reimplement the selection, the TOP_N cut, the tie-break, or the band. Call paper.target_portfolio and copy the band expression verbatim (`abs(delta) < max(25.0, notional * 0.1)`). The instant the shadow and real arms use different code paths, the cross-arm comparison stops measuring the profile and starts measuring the divergence. The monkeypatch test in step 9 exists to catch a future 'small' divergence.
- The symbology seam is real and asymmetric: panels use the SEC DASH form (BRK-B) and market_eod uses the POLYGON DOT form (verified: 'BRK.B' present, 'BRK-B' absent, 89 dotted symbols on 2026-07-28). Use _to_broker_symbol for every archive lookup and _to_panel_symbol for every state key, and keep paper.py's post-normalization collision check.
- HANDOFF rule 2 (the mistake made twice this week): never wrap a documented JSON schema in an envelope. Do not restructure the existing rebalance/snapshot records to 'make room' for the arm field — add keys additively, and make arms.load_arm treat a MISSING 'arm' as 'alpaca_paper' so the reader works before paper.py is touched at all.
- HANDOFF rule 3 (the 'DOA workflow' bug class): --panel-source, --profiles, --rebuild, --profile must be registered on their SUBPARSERS with parents=[shared], not on the top-level parser. The workflow passes them subcommand-first; a top-level-only registration silently kills every scheduled run.
- Real snapshot position fields are STRINGS (Alpaca serializes account numbers as strings; journal_snapshot passes the payload through unchanged) while equity/cash are floats (they go through _account_value). If the shadow emits floats for position fields, a shared reader has to branch on arm — defeating the design. Emit fixed-format strings and coerce in one _num() helper.
- paper.py and Stock-Grader/src/stock_grader/cli.py are being edited concurrently, and paper.py is gaining {'kind':'benchmark'} and {'kind':'fill'}. Re-read both immediately before writing code (`git -C <vault> diff src/stock_vault/paper.py`, `git -C <grader> diff src/stock_grader/cli.py`). If the real fill/benchmark records land with different key names than this spec, adopt the REAL names — one reader beats a prettier schema — and never revert another agent's edit.
- Cost auto-tuning would be a silent history rewrite. calibrate() must be report-only. Changing COST_BPS requires bumping SIM_VERSION, which changes the arm directory and starts a fresh arm; existing arms stay byte-frozen. If you ever find yourself editing a constant so old journals 'look better', stop.
- The archive is 25 month dirs x ~20 days x ~12,500 rows. A cold --rebuild across 11 arms must iterate SESSION-MAJOR (outer sessions, inner arms) so each day file is parsed once; an arm-major loop parses everything 11 times and will blow the workflow timeout. Bound the closes() cache to a handful of days so a rebuild does not hold ~6M rows in memory.
- Scheduling order matters: collectors.yml writes day D-1's EOD at 22:23 UTC, so a shadow run before that (e.g. paper-trader.yml's 15:37 UTC slot) has nothing new to fill against. Schedule shadow-arms.yml after the EOD write (cron '11 23 * * 1-5') and bootstrap-guard the staleness gate exactly as the paper clock already is, or the very first run fails on a directory that does not exist yet.
- LICENSING (ECOSYSTEM rule 5): shadow journals are derived from Massive/Polygon free-tier bars and private frozen panels. They belong ONLY in the private Stock-Vault, must carry a license_note saying so, and must never be published, mirrored, or summarized into a public repo. Decide placement before the first write — public git history is forever.
- Do not let the simulator borrow. The real Alpaca account would reject an order it cannot fund; a sim that silently goes negative on cash manufactures returns the real arm could never earn. Clamp to available cash and journal the shortfall as a `failed` leg.

## Acceptance criteria

Do not report this milestone complete until every box is checkable.

- [ ] `cd C:/Users/tforstrom/Desktop/Stock-Vault && pip install -e ".[dev]" && pytest -q` is green, and `ruff check src tests` is clean (line-length 100, select E,F,W,I,UP,B).
- [ ] `python -c "import ast,sys; t=ast.parse(open('src/stock_vault/shadow.py').read()); assert not [n for n in ast.walk(t) if isinstance(n,ast.Attribute) and n.attr in {'now','today','utcnow'}], 'wall clock in shadow.py'"` exits 0. Test `test_no_wall_clock_in_shadow` asserts the same plus that executed_utc / captured_utc / filled_at_utc are None on every emitted shadow record.
- [ ] DETERMINISM, the load-bearing criterion: `stock-vault shadow-run --vault-dir data --panel-source ../Stock-Grader` run twice in a row leaves every data/shadow_journal/**/ *.jsonl.gz byte-identical after the second run, and a `--rebuild` into a clean tree produces the same sha256 for every journal file as the incremental run. Covered by test_rerun_is_byte_identical_to_a_full_rebuild and test_second_run_with_no_new_sessions_writes_nothing.
- [ ] ON DISK after a first real run against the live tree: data/shadow_journal/<profile>/v1-top10-equal-monthly__sim-v1-nextclose-5bps/ exists for every profile that has a frozen panel, each containing arm.json, manifest.json, at least one YYYY-MM.jsonl.gz, and a hidden .state.json that write_manifest does NOT list (manifest.py:33 skips dot-prefixed names — assert 'name' values in manifest['files'] contain no '.state.json').
- [ ] `stock-vault arms --vault-dir data --format json` lists 'alpaca_paper' plus one shadow arm per profile with panels, and equity_curve() returns a non-empty series for every shadow arm. The real arm's existing records (which have no 'arm' key) are still returned — test_reader_serves_both_arms_and_defaults_missing_arm.
- [ ] SHARED SHAPES: for a shadow rebalance record R and the real record shape, set(R) >= {'kind','rules_version','signal_date','executed_utc','equity_before','targets','actions'}; for a shadow snapshot S, set(S) >= {'kind','date','captured_utc','equity','cash','positions'}, with every value in S['positions'][i] a str and S['equity'], S['cash'] floats. Asserted by test_records_share_the_real_journal_shapes.
- [ ] RULES PARITY: test_shadow_calls_the_pre_registered_selection_function proves shadow.py invokes paper.target_portfolio itself (monkeypatched counter fires) and that its picks match paper.target_portfolio(rows, equity) exactly. `grep -n 'TOP_N\|sort(key' src/stock_vault/shadow.py` shows no reimplementation of the selection or tie-break.
- [ ] VERSION STAMPING: the arm directory name equals f'{RULES_VERSION}__{SIM_VERSION}', arm.json records rules_version / sim_version / cost_bps / top_n / max_weight / max_panel_age_days / start_equity / first_signal_date, and monkeypatching shadow.COST_BPS without bumping SIM_VERSION raises ShadowError whose message names SIM_VERSION (test_arm_dir_is_version_stamped).
- [ ] FILL MODEL: against a two-session fixture (panel 2026-07-30, archive 2026-07-30 and 2026-07-31), every fill record has date == '2026-07-31', price == round(reference_close*(1+5.0/1e4), 4) for buys and round(reference_close*(1-5.0/1e4), 4) for sells, and cost_bps == 5.0. No fill is ever dated on or before its signal_date.
- [ ] CALIBRATION: `stock-vault shadow-calibrate --vault-dir data --profile all_weather` exits 0 today and prints {"pairs": 0, ...} because the live paper_journal contains only snapshot records. Once real {"kind":"fill"} records exist, it prints pairs>0 with realized_bps_median and suggested_cost_bps, and asserts (in test) that COST_BPS is unchanged on disk after the call.
- [ ] STALENESS: 'shadow' appears in stock_vault.staleness.DATASETS; `stock-vault check-staleness --vault-dir data shadow` prints a 'shadow: fresh through ...' line after a run and exits 1 with a StalenessError message after 6+ simulated days (test_shadow_clock_uses_arm_manifest_timestamp, test_shadow_missing_arms_is_loud).
- [ ] REAL-TRADER REGRESSION: `stock-vault paper-rebalance --panel-source ../Stock-Grader --vault-dir data` no longer raises 'no frozen panels under ...' against the per-profile layout; test_per_profile_panel_layout_is_found and test_legacy_flat_panel_is_matched_by_profile_column both pass.
- [ ] SAFETY/INTEGRITY: test_market_eod_reader_refuses_unlisted_or_tampered_file passes all three cases (flipped byte, unlisted file, schema_version '2.0'). No shadow code path calls the network, and `grep -rn 'requests\|urllib' src/stock_vault/shadow.py src/stock_vault/eod.py src/stock_vault/arms.py src/stock_vault/journal.py` returns nothing.
- [ ] WORKFLOW VERIFIED LIVE, not just committed: after pushing, `gh run list --repo TylerJForstrom/Stock-Vault --limit 5` shows shadow-arms completing successfully (HANDOFF rule 5), and the committed diff contains new data/shadow_journal/ files.
