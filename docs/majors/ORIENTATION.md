# Orientation: the Stock-Data / Stock-Vault / Stock-Grader ecosystem

> What a stranger must know before touching anything. Every claim was verified by reading the code.

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

## Topic 1 — The three repos: purpose, entry points, install, test suite, current test count

There are three git repos on this Windows machine, all rooted at C:/Users/tforstrom/Desktop/. None imports another's code; they are separate installable Python packages.

**Stock-Data (PUBLIC foundry)** — C:/Users/tforstrom/Desktop/Stock-Data. Role per ECOSYSTEM.md: the data foundry, the project that fetches public-domain external sources and publishes point-in-time, provenance-carrying artifacts. Package `stock_data` under src/. Entry point: console script `stock-data = stock_data.cli:main` (pyproject.toml). Subcommands, all defined in src/stock_data/cli.py:26-56: `snapshot-symbols` (archive the four symbol directories + emit diff events), `corporate-actions --tickers ...` (reconstruct dividends/splits from SEC XBRL companyfacts), `events` (sweep 8-K red-flag items + Form 25/15 delisting filings), `check-staleness --max-age-days N DIR...` (the workflow gate). Runtime deps are only requests, pandas, pyarrow. Install and test:
```
cd C:/Users/tforstrom/Desktop/Stock-Data
pip install -e ".[dev]"
ruff check .
pytest
```
Current count: **38 tests** across tests/test_cli.py, test_corporate_actions.py (17), test_events.py (6), test_manifest.py (3), test_symbols.py (9). CI (.github/workflows/ci.yml) runs ruff + pytest on ubuntu-latest AND windows-latest, Python 3.12, on every push to main and every PR.

**Stock-Vault (PRIVATE vault)** — C:/Users/tforstrom/Desktop/Stock-Vault. Role: the private twin of the foundry. It holds collectors and archives for sources whose terms forbid public redistribution, plus the Alpaca **paper** trader. Its README opens with 'This repository must never be made public.' Package `stock_vault` under src/. Entry point: `stock-vault = stock_vault.cli:main`. Subcommands (src/stock_vault/cli.py:44-97): `harvest-delisted --years ...`, `market-eod --backfill-days N`, `borrow`, `ssga`, `paper-rebalance --panel-source <grader clone>`, `paper-journal`, `recs --universe-file ...`, `finra-short-interest --since DATE`, `check-staleness [--pre-collection] DATASET...`. Runtime dep is only requests; pandas/pyarrow arrive via the `dev` or `paper` extras. Install and test:
```
cd C:/Users/tforstrom/Desktop/Stock-Vault
pip install -e ".[dev]"
pytest
```
Current count: **45 tests** — test_collectors.py 9, test_finra.py 4, test_ops_hardening.py 17, test_paper.py 15. **Know this: Stock-Vault has no CI workflow.** Its .github/workflows/ holds only collectors.yml, monthly-recs.yml, paper-trader.yml. Nothing runs those 45 tests except a human on this machine, so run them yourself before every commit here.

**Stock-Grader (PUBLIC engine)** — C:/Users/tforstrom/Desktop/Stock-Grader. Role: the system of record for grading methodology — the scoring engine, the backtest evaluator, the significance/ledger machinery, and the append-only frozen_scores/ panels. Package `stock_grader` under src/. Entry point: `stock-grader = stock_grader.cli:main`. Subcommands (src/stock_grader/cli.py:1111-1204): `grade`, `rank`, `freeze`, `consensus`, `research`, `backtest`, `methods`, `metrics`. There are deliberately no standalone `peers` or `valuation` commands; those are composed by `research`. Requires Python >=3.11; runtime deps numpy, pandas, scipy, scikit-learn, statsmodels, requests, rich, pyarrow. Install and test:
```
cd C:/Users/tforstrom/Desktop/Stock-Grader
pip install -e ".[dev]"    # NOT optional: two subprocess tests import the installed live tree
pytest -q
```
Current count: **574 tests**. Full-suite runtime is roughly 20 minutes (HANDOFF.md), so batch: targeted tests per chunk, full suite every 2-3 chunks, in the background. CI (.github/workflows/ci.yml) has three jobs. `test`: matrix ubuntu × py3.11/3.12/3.13 plus windows × 3.12, and it runs `pytest -q --cov=stock_grader --cov-fail-under=55`. That 55% ratchet is blocking and is declared twice (pyproject [tool.coverage.report] and the CI flag); never lower it to make a change merge. Lint/format/type checks are split in two: a **blocking** 'hardened surface' pass over exactly src/stock_grader/data/prices.py, data/sec_prices.py, weighting.py, tests/test_production_hardening.py, and an **advisory** (continue-on-error) repository-wide pass that carries known debt. `package`: builds sdist+wheel, twine check, installs the wheel, smoke-tests `stock-grader --help` and asserts the bundled config/universe_default.txt is packaged. `security`: pip-audit and `bandit -q -r -ll src scripts` blocking, low-severity bandit advisory.

Repo-local reading order for a stranger, in this order: Stock-Data/ECOSYSTEM.md (the contract), Stock-Grader/AGENTS.md (agent rules), Stock-Grader/docs/HANDOFF.md (the live work queue and bug classes), Stock-Grader/docs/REVIEW_FEEDBACK.md (recurring reviewer findings; address [blocking] items before new feature work), then Stock-Grader/docs/REVISED_PLAN.md (the authoritative plan).

## Topic 2 — The artifact contract: manifest.json, schema_version enforcement, hash verification, and what 'consumers refuse unknown schemas' means in code

Rule 1 of ECOSYSTEM.md is 'Artifacts, not imports.' Repos integrate only through published dataset directories, each carrying a `manifest.json`. No repo imports another repo's code — this is verified true today; the only cross-repo couplings are a raw-URL fetch, a GitHub Actions checkout, and a local clone path.

**The manifest fields.** Written by `write_manifest()` in Stock-Data/src/stock_data/manifest.py:33-70 (and the byte-identical twin in Stock-Vault/src/stock_vault/manifest.py:23-49):
- `schema_version` — currently the string `"1.0"`, from the module constant SCHEMA_VERSION.
- `generated_at_utc` — `'%Y-%m-%dT%H:%M:%SZ'`.
- `source_urls` — sorted list of the exact upstream URLs.
- `license_note` — the license that travels with the data (see Topic 4).
- `files[]` — one entry per data file in the directory: `{name, sha256, bytes}`, plus `{rows}` when the caller passed row_counts. manifest.json itself and any dotfile are excluded.
- Anything else the producer passes via `extra` is merged at top level, **additively**. Real examples on disk today: `snapshot_date`, `failures`, `last_success` (data/symbols/current), `last_checked_date` (data/symbols/events), `basis: current_fully_split_adjusted`, `granularity: 'fiscal_period (no ex-dates in XBRL)'`, `tickers_requested`, `tickers_unresolved` (data/corporate_actions).

**Atomic publication.** `publish_staged_dataset()` (Stock-Data manifest.py:73-125) exists because writing data first and hashing after leaves a window where a kill publishes data whose manifest sha256s don't match — and consumers then hard-refuse the dataset until the next successful write. So it stages EVERYTHING (new bytes plus copies of untouched existing files, because the manifest must still cover them) in `<dataset>/.staging`, computes the manifest from those exact staged bytes, then renames the new data files, then renames manifest.json LAST. `.staging` is gitignored so a killed publish cannot be committed by `git add -A data`.

**What 'consumers refuse unknown schemas' means in code.** It means a raised exception, not a warning, in three named functions:
1. `FoundryDataSource.manifest()` — Stock-Grader/src/stock_grader/data/foundry.py:79-88. Compares against `SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})` (line 31) and raises `FoundryError(f"unknown foundry schema_version {version!r} in {dataset_dir} (supported: ...); refusing to read")`.
2. `VaultDataSource._manifest()` — Stock-Grader/src/stock_grader/data/vault.py:50-64. Same frozenset (line 31), raises `VaultError(... 'refusing to read')`.
3. Producer-side, `read_manifest()` — Stock-Data/src/stock_data/manifest.py:128-136 raises ValueError on any other version. `manifest_generated_at()` (139-155) builds on it and is what `stock-data check-staleness` calls, so a schema bump also reds every scheduled gate.

**Hash verification** is the second half of the same contract, enforced in `FoundryDataSource._read_dataset_file()` (foundry.py:90-104) and `VaultDataSource._read_verified()` (vault.py:66-76). Both do two checks: (a) the requested filename must appear in `manifest['files']` — `FoundryError(f"{name} is not listed in {dataset_dir}/manifest.json")` — so an unmanifested file is unreadable no matter that it sits right there on disk; and (b) `hashlib.sha256(blob).hexdigest()` must equal the manifest's `sha256` — the mismatch message quotes both hashes. `verify_hashes=False` exists as a constructor escape hatch; do not use it in production paths. FoundryDataSource additionally refuses a relpath that escapes the foundry root (foundry.py:56-58).

**Two consequences a stranger must internalize.** First: because a filename must be manifested to be readable, writing a data file outside a manifest makes it invisible to the whole ecosystem forever. This is exactly the open bug in HANDOFF item 5 for events.jsonl (now fixed — data/symbols/events/ has its own manifest.json listing events.jsonl with rows: 628). Second: because sha256 covers exact bytes, line-ending translation silently breaks every consumer. Both repos pin it: Stock-Data/.gitattributes sets `*.jsonl text eol=lf`; Stock-Vault/.gitattributes sets `data/** -text` with the comment that it disables conversion in BOTH directions (Windows checkout smudging and commit-time normalization on Linux runners).

**Bumping schema_version is a two-repo, four-file operation.** SCHEMA_VERSION in Stock-Data/manifest.py, SCHEMA_VERSION in Stock-Vault/manifest.py, SUPPORTED_SCHEMA_VERSIONS in foundry.py, SUPPORTED_SCHEMA_VERSIONS in vault.py. Bump one and every consumer hard-refuses. The preferred move is almost always to add keys additively via `extra` and leave the version at 1.0.

## Topic 3 — The one-direction DAG: which repo may talk to which, and the exact code edges that implement it

ECOSYSTEM.md Rule 2: **sources → foundry → grader → backtest → forward. No cycles.** The full membership table in ECOSYSTEM.md lists five projects, of which three are on this machine:
- **Stock-Data** — foundry; fetches external sources, publishes artifacts. (Stock-Vault is its private twin: same role, restricted sources, private storage.)
- **Stock-Grader** — system of record for grading methodology; consumes foundry artifacts, produces score panels, research dossiers, and the backtest attestations.
- **TickerPulse** — sentiment/attention source. Its derived per-ticker daily metrics are archived **by the foundry**; raw social content is not. TickerPulse metrics must enter the grader through the foundry, never directly.
- **Stock Market Simulation** — forward/execution layer and test oracle. Its known-answer tapes calibrate the backtest harness's power and false-positive rate; they must never become production data. It does not validate alpha.
- **Stock-Rater** — ARCHIVED by owner decision 2026-07-28. Not a second grading engine. Do not revive it as one.

The DAG is not aspirational; here are the actual edges in code:

1. **Grader reads Data (public foundry).** `FoundryDataSource(root=<local clone>)` or `FoundryDataSource(url_base=...)` — exactly one, enforced in the constructor (foundry.py:45-46). The default raw base is `_FOUNDRY_RAW_URL = "https://raw.githubusercontent.com/TylerJForstrom/Stock-Data/main"` (cli.py:98). Two user-facing surfaces: `--universe foundry:` / `foundry:C:/path/to/Stock-Data` / `foundry:https://...` resolves the listed-exchange universe (cli.py:100-118), and `--foundry` supplies reconstructed dividends-per-share as a fallback for dividend metrics (cli.py:395-408, 1103-1105). Datasets exposed: `events()`, `universe(listed_only=, asof=)`, `universe_tickers()`, `dividends()`, `splits()`, `trailing_dps()`, and the TickerPulse mirror via `sentiment_days()`, `sentiment_trends(day)`, `sentiment_buckets(day)` (public aggregate counts/scores; the file dated D is knowable before D's US close, so D is the earliest usable signal date).
2. **Grader reads Vault (private) — local clone only.** `VaultDataSource(root=...)`; there is no url_base mode, and the docstring says why: 'the repo is private and its license notes prohibit anything that would put raw vendor data behind a URL.' It raises if `<root>/data` is not a directory. This is the survivorship-proof price layer: `market_eod_day()`, `market_eod_available_days()`, `market_eod_series()`, borrow fees, delisted histories.
3. **Vault reads Grader — as artifacts, never imports.** `.github/workflows/paper-trader.yml` does a second `actions/checkout@v4` of TylerJForstrom/Stock-Grader into `path: grader`, then runs `stock-vault paper-rebalance --vault-dir data --panel-source grader`, which reads the frozen parquet panels off that checkout. `.github/workflows/monthly-recs.yml` curls the grader's raw `config/universe_default.txt` to build the recommendation universe.
4. **Nothing else.** No Python module in any repo imports a module from another repo. Do not add one, however convenient — HANDOFF item 4 explicitly instructs copying a ~20-line ticker helper into each repo rather than importing across the boundary.

The direction rules that bite: PIT symbol/event data enters the grader through FoundryDataSource, not by re-fetching SEC in the grader; restricted vendor prices enter through VaultDataSource from a local clone; simulation tapes feed only the backtest calibration harness. Anything that would make the foundry read the grader, or the grader publish back into the foundry, is a cycle and is out of contract.

**One load-bearing operational coupling:** Stock-Grader must stay a PUBLIC repo. paper-trader.yml checks it out with the workflow's default GITHUB_TOKEN, which cannot read *other* private repositories. If Stock-Grader is ever made private, that checkout step needs a fine-grained PAT (Stock-Grader contents: read) passed via the step's `token` input, or the whole paper clock dies at checkout. This is documented in Stock-Vault/README.md.

## Topic 4 — The licensing split: exactly what may be published, what may never leave the vault, and the consequence of getting it wrong

ECOSYSTEM.md Rule 5: 'US-government public-domain data and derivations may live in public repos. FINRA, Tiingo, Stooq, and anything derived from restricted data stays in gitignored vaults or private storage. **Decide placement before first write — public git history is forever.**'

**Public-domain-publishable (belongs in Stock-Data, a public repo).** All four archived sources plus everything computed from them:
- SEC `https://www.sec.gov/files/company_tickers.json` and `company_tickers_exchange.json`
- Nasdaq Trader `nasdaqlisted.txt` and `otherlisted.txt` (exchange reference directories)
- SEC XBRL companyfacts (`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`) — the source of the reconstructed dividends/splits
- SEC submissions JSON (`https://data.sec.gov/submissions/CIK##########.json`) — the source of the 8-K red-flag and Form 25/15 delisting event stream
Every one of these carries a single license note, the module constant PUBLIC_DOMAIN_NOTE (Stock-Data/src/stock_data/manifest.py:20-22): *"US-government public-domain source data (17 USC 105); derived work, freely redistributable."* If a new foundry dataset cannot honestly carry that exact string, it does not belong in Stock-Data.

**Private-vault-only (belongs in Stock-Vault, which must never be made public).** Each collector declares its own LICENSE_NOTE constant, and these are the operative terms — read them as the rule, not the README summary:
- **stockanalysis.com** (delisted price cohorts) — delisted.py:31: 'republication in full prohibited by site ToS; private research archive only. Derived values may be published with attribution.'
- **Massive / ex-Polygon** (whole-market EOD OHLCV) — market_eod.py:22: 'free-tier data: personal use; read current terms before any redistribution. Private archive.' Terms pages were not machine-fetchable when this was built; check them yourself before publishing anything derived.
- **Interactive Brokers shortable feed** (borrow fee/availability) — borrow.py:26: 'no explicit license; private research archive, publish derived aggregates only.'
- **Finnhub free tier** (analyst recommendation trends) — recs.py:21: **'no redistribution of data or derived results; delete on subscription end.'** This is the strictest one: *derived* results are covered too, so a score panel that consumed Finnhub inputs cannot be published either. The Yahoo redundancy module is an unofficial endpoint, private use only.
- **SSGA** (SPDR daily holdings XLSX) — ssga.py:19: 'reproduction clause in site terms; private research archive only.'
- **FINRA** (twice-monthly short interest) — finra.py:22: 'non-commercial internal use only; redistribution prohibited.'
- **Alpaca paper journal** — paper.py:41: 'private forward-evidence record; never redistribute.'

**Defense in depth already in place.** Stock-Data/.gitignore ignores `vault/` with the comment 'restricted-license data belongs in Stock-Vault and must NEVER be committed to this public repository.' The FINRA collector that once lived (as dead code) in Stock-Data has been removed — src/stock_data/ now contains only __init__, cli, corporate_actions, events, http, manifest, symbols — and FINRA collection is invoked solely from Stock-Vault's collectors.yml. Do not re-add a restricted collector to Stock-Data 'temporarily'.

**The consequence of getting it wrong.** It is unrecoverable and it is not merely a policy problem. Committing restricted data to a public repo publishes it the moment the workflow pushes; `git push --force` after a history rewrite does not retract it, because forks, clones, GitHub's own commit cache, and any scraper already have the bytes. For Finnhub specifically, the terms cover derived results, so a leak can contaminate the publishable status of downstream score panels, not just the raw file. And the scheduled workflows push automatically (`git add -A data` + commit + 3-attempt rebase-retry push), so a misplaced write does not wait for a human to notice — it ships on the next cron tick. Therefore: decide placement **before the first write**, and when in doubt, write it to the vault. Moving data from private to public later is trivial; the reverse is impossible.

## Topic 5 — The autonomous clocks: every workflow across the three repos, its cron, what it does, and its staleness gate

Eight workflow files exist across the three repos. Six are scheduled ('clocks'); two are CI. ECOSYSTEM.md Rule 4 explains why the clocks outrank everything: 'Forward-only clocks run first. Anything that cannot be backfilled (PIT symbol snapshots, sentiment metrics, paper-trade journal) starts before anything that can. Every day of delay is unrecoverable data.'

All six scheduled workflows share one design, and you should preserve it in anything you add: **the staleness gate runs BEFORE collection**, so a missed cadence turns the scheduled run red (GitHub's failure email to the repo owner IS the alerting — there is no external monitoring service and none is wanted); the collection steps then run under `if: always()` so the archive still self-heals in the same run; the commit step is also `if: always()` and pushes with a three-attempt `git pull --rebase` retry so a concurrent commit on main cannot discard a snapshot. Crons deliberately sit on odd minutes because GitHub cron is best-effort (5-60+ min jitter, occasional skips).

**Stock-Data / .github/workflows/ci.yml** — no cron. Triggers on push to main and on pull_request. Matrix ubuntu-latest + windows-latest, Python 3.12: `pip install -e ".[dev]"`, `ruff check .`, `pytest`. No staleness gate (not a clock).

**Stock-Data / daily-snapshot.yml** — cron `"17 7 * * *"` (daily 07:17 UTC), plus workflow_dispatch. permissions: contents: write; concurrency group daily-snapshot, cancel-in-progress: false. Gate: `stock-data check-staleness --max-age-days 2 data/symbols/current data/symbols/events`, plus cross-coverage `stock-data check-staleness --max-age-days 8 data/events` when that directory exists (so a disabled weekly workflow cannot fail silently — the weekly clock is checked daily). Work: `stock-data snapshot-symbols --data-dir data`. Commit step is a **heartbeat that always commits**, explicitly because GitHub disables cron workflows after 60 days without commits — the heartbeat records the failure and resets that timer, so the archive cannot silently stop.

**Stock-Data / weekly-events.yml** — cron `"43 8 * * 6"` (Saturdays 08:43 UTC), plus dispatch. timeout-minutes: 120. Gate: `stock-data check-staleness --max-age-days 8 data/events` (eight days tolerates cron jitter but catches a skipped weekly run). Work: `stock-data events --data-dir data` — the 8-K red-flag and Form 25/15 delisting sweep. Commit message `events: $(date -u +%G-W%V)`.

**Stock-Vault / collectors.yml** — TWO crons: `"23 22 * * 1-5"` (22:23 UTC weekdays, after the US close: EOD + borrow) and `"41 14 * * 1-5"` (14:41 UTC, the intraday borrow snapshot). workflow_dispatch takes a `backfill_days` input (default "35"; 730 = full free-tier history, ~2h). timeout-minutes: 180. Gate: `stock-vault check-staleness --vault-dir data --pre-collection borrow market-eod finra recs`, plus a bootstrap-guarded `... paper` check when data/paper_journal exists. Steps, all `if: always()`: `borrow` (continue-on-error — port-21 egress is flaky and must not block EOD), `market-eod --backfill-days "${{ inputs.backfill_days || '35' }}"` with MASSIVE_API_KEY, `finra-short-interest --since "$(date -u -d '120 days ago' +%F)"`, `ssga` (continue-on-error, informational), a `date -u > data/heartbeat.txt` heartbeat, then commit.

**Stock-Vault / monthly-recs.yml** — cron `"37 12 2 * *"` (12:37 UTC on the 2nd of each month). timeout 60. Gate: `stock-vault check-staleness --vault-dir data recs`, bootstrap-guarded (skipped when no finnhub_*.jsonl.gz exists yet). Then it curls Stock-Grader's raw `config/universe_default.txt` with `--fail --silent --show-error --location` and an awk check that the file contains at least one non-comment ticker; then `stock-vault recs --vault-dir data --universe-file universe.txt` with FINNHUB_API_KEY, followed by a hard check that `data/rec_trends/finnhub_$(date -u +%Y-%m).jsonl.gz` exists and is non-empty.

**Stock-Vault / paper-trader.yml** — cron `"37 15 * * 1-5"` (15:37 UTC weekdays, ~11:37 ET). timeout 30. Checks out Stock-Vault, then Stock-Grader into `path: grader`; `pip install -e .[paper]`. Gate: `stock-vault check-staleness --vault-dir data paper` when data/paper_journal exists. Then `stock-vault paper-rebalance --vault-dir data --panel-source grader` (idempotent — it only trades when a NEW panel signal_date appears) and `stock-vault paper-journal --vault-dir data`, both with ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET.

**Stock-Grader / ci.yml** — no cron. push to main + pull_request; concurrency cancels in-progress runs per ref. Three jobs (test / package / security) described in Topic 1.

**Stock-Grader / monthly-freeze.yml** — cron `"19 13 1 * *"` (13:19 UTC on the 1st of each month), plus dispatch. timeout 120. It has **no staleness gate of its own**; instead it fails fast when `STOCK_GRADER_CONTACT` (from the `SEC_CONTACT_EMAIL` repository secret) is empty, rather than degrading to a placeholder SEC User-Agent. Work: `stock-grader freeze --profile all_weather --universe config/universe_default.txt --out frozen_scores`, then commit `freeze: $(date -u +%Y-%m)`. Note this still passes `--profile all_weather` while cmd_freeze already supports `--all-profiles` (all 11 profiles off one snapshot set, zero extra network calls) — that switch is a pending edit.

**The staleness thresholds live in code, not YAML.** Stock-Vault/src/stock_vault/staleness.py: BORROW_MAX_AGE 3 days; RECS_MAX_AGE 35 days (and the recs check also requires the newest artifact to actually be listed in rec_trends/manifest.json); FINRA_MAX_AGE 45 days (FINRA publishes each settlement 2-4 weeks late; 45 tolerates one missed cycle); PAPER_MAX_AGE 6 days (weekday cadence + a missed Friday + weekend + a Monday holiday — 4 was a knife edge). market-eod is not age-based but expectation-based: `previous_market_day()` walks back over weekends and a computed NYSE holiday set (including Good Friday via the Gregorian computus and Juneteenth from 2022), and `pre_collection=True` grants exactly one extra market day of grace because day D's run is the first that can write D-1. Every check derives its clock from an artifact filename or manifest content, never from file mtimes — 'checkout mtimes describe the checkout, not the observation.' `check_datasets()` collects all failures and raises them in one exception so one red run names every stale clock.

Stock-Data's gate is different in kind: `stock-data check-staleness` compares `manifest_generated_at()` against `--max-age-days`, AND separately audits the per-source `last_success` watermark map. A source in SOURCES with no watermark at all is reported STALE ('a collector broken from day one never writes one and would stay green forever'), and a watermark older than threshold + 2 days is STALE. That is what stops a single failing source from hiding behind the manifest's always-fresh write timestamp.

## Topic 6 — Working rules and known bug classes (docs/HANDOFF.md), stated as imperatives

These are recorded in Stock-Grader/docs/HANDOFF.md under 'Working rules (non-negotiable, learned the hard way this week)', plus Stock-Grader/AGENTS.md. Each was written after something broke. Treat them as imperatives, not advice.

1. **Commit one coherent chunk at a time, and keep the suite green before AND after every commit.** Run `pip install -e ".[dev]"` first — two subprocess tests import the live installed tree, so a stale install produces phantom failures. Never leave the suite red between commits.

2. **Never wrap a documented JSON schema in an envelope. Add keys additively.** This mistake was made twice in one week; the tests caught it both times. The pattern to copy is in cmd_backtest: `payload = json.loads(to_json(report))` then `payload["significance"] = ...` and `payload["ledger"] = ...`, with the in-code comment 'Additive keys on the documented report schema - never an envelope.' (cli.py:859-865). The same rule governs manifests: extend via `extra`, never restructure, never bump schema_version to sneak in a shape change (see Topic 2).

3. **Register every CLI flag a workflow passes on the SUBPARSER, using the `parents=[shared]` pattern.** The 'DOA workflow' bug class: workflows invoke `stock-data snapshot-symbols --data-dir data` and `stock-vault market-eod --vault-dir data`, i.e. subcommand first. A top-level-only argument silently killed every scheduled run in two repos. The correct pattern is live in both CLIs — a `shared = argparse.ArgumentParser(add_help=False)` carrying `--data-dir` / `--vault-dir`, passed as `parents=[shared]` to each subparser (Stock-Data cli.py:18-24, Stock-Vault cli.py:36-42). If you add a flag that any YAML passes, add it to the subparser and prove it with `stock-<x> <subcommand> --your-flag ...`.

4. **Obey the PowerShell 5.1 rules.** Never `2>$null` a native command under `$ErrorActionPreference = "Stop"` — PS 5.1 wraps native stderr in a NativeCommandError and turns a successful exit 0 into a terminating failure; the working workaround in the repo is `cmd /c "gh auth status >nul 2>&1"`. And **never pipe a secret to a native command's stdin**: PowerShell appends CRLF, which corrupted every stored GitHub secret once (the API then 401s on `key+%0D%0A`). Always pass secrets as an argument: `gh secret set $NAME --repo $Repo --body $value`, exactly as scripts/set-secrets.ps1 does, with that comment attached at both call sites.

5. **After dispatching any workflow, VERIFY the run completed.** `gh run list --repo TylerJForstrom/Stock-Data --limit 5` (and the same for Stock-Vault / Stock-Grader). A dispatched-but-dead run burned a full day of the point-in-time archive, which cannot be backfilled. 'I dispatched it' is not evidence; a green run is.

6. **Budget for the suite.** ~20 minutes for Stock-Grader's 574 tests. Run targeted tests per chunk and the full suite every 2-3 chunks in the background; fix forward if it goes red.

From AGENTS.md, additionally: read docs/REVIEW_FEEDBACK.md at the start of each session and clear anything marked **[blocking]** before new feature work, and append a one-line entry to its 'Agent log' when you finish a queue item, with commit hashes. Do not add new weighting methods, aggregators, or attribution/interval precision (there is an explicit 'stop doing' list at the end of docs/REVISED_PLAN.md). Never commit `.coverage` or other generated artifacts. Every user-facing output must carry the not-investment-advice framing — grades, valuations, and model output are never presented as predictions or recommendations.

Two further bug classes worth knowing because they are structural, not stylistic: (a) **unmanifested files are invisible** — writing a data file outside its dataset manifest means no consumer can ever read it (Topic 2); (b) **line-ending translation silently breaks sha256 verification**, which is why both repos pin .gitattributes.

Finally, a note that was written while two files were mid-edit and is now **resolved**: Stock-Vault/src/stock_vault/paper.py and Stock-Grader/src/stock_grader/cli.py are both merged and pushed. The grader writes the per-profile layout `frozen_scores/<profile>/<YYYY-MM-DD>.parquet` (nine panels for 2026-07-30), paper.py reads it via `load_frozen_panel(source, profile="all_weather")` with a documented legacy flat fallback, and the paper journal emits four record kinds: `rebalance`, `snapshot`, `benchmark`, and `fill`. Both files are safe to read and edit normally — but re-read them, because every line number in these specs predates the merge.

## Topic 7 — The statistical-honesty machinery: the append-only research ledger, its hash chain, deflated Sharpe / PSR, and why you must never delete a line or silently add a trial

This is the part of the ecosystem that exists to stop you from fooling yourself, and it is the part a stranger is most likely to break by accident.

**The problem it solves.** A high backtest Sharpe means almost nothing on its own. If you tried many configurations and kept the best, the winner's Sharpe is inflated by *selection*; and the Sharpe statistic itself is distorted by skew and fat tails. src/stock_grader/significance.py corrects for both (Bailey & Lopez de Prado, 2012/2014, ported from the simulation project's harness with its positive/negative-control tests):
- `probabilistic_sharpe_ratio(returns, benchmark_sr)` — P(true per-period Sharpe > benchmark), skew- and kurtosis-aware.
- `expected_max_sharpe(trial_sharpe_std, n_trials)` — the expected MAXIMUM Sharpe under the null of no skill across N trials, using EULER_MASCHERONI = 0.5772156649015329. This is the bar a best-of-N Sharpe must clear.
- `deflated_sharpe_ratio(returns, trial_sharpes) -> (dsr, benchmark)` — PSR against that expected-max benchmark.
- `block_bootstrap_sharpe_ci(...)` — circular block bootstrap on the ANNUALIZED Sharpe, preserving autocorrelation. (All PSR/DSR inputs are per-period, not annualized; the helpers keep that straight — do not mix the two.)
- `assess_edge(...) -> SignificanceReport`, where `significant = dsr >= (1 - alpha) and ci_low > 0`, and the verdict is one of 'INSUFFICIENT SAMPLE' (n < 30), 'EDGE', 'MARGINAL' (dsr >= 0.90), 'NO EDGE'. These are diagnostics, never optimisation targets — do not tune anything to raise DSR.

**The ledger.** `research_ledger.jsonl` at the Stock-Grader repo root (the `--ledger` default; it resolves relative to CWD). One JSON object per line, written by `append_record()` in src/stock_grader/research_manifest.py. Each `ResearchRecord` captures exactly what the honesty firewall cares about: experiment, market, symbols, targets, horizons, **trials** (declared grid size for multiple-testing correction), metrics, costs, benchmark, leakage_controls, gate_passed, verdict, data_span, code_commit, created_utc, prev_sha256. Metrics are `float | None` and never NaN — a serialized NaN poisons every aggregate computed over the ledger later, so non-finite values are stored as JSON null (research_manifest.py:71-74; cli.py:824-837; the mirror-image guard is `_finite_trial_sharpes` in significance.py:161-167, whose comment is 'one NaN makes stdev — and so every DSR — NaN').

**The hash chain.** `payload()` produces a canonical view (`json.dumps(..., sort_keys=True, separators=(",", ":"))`), `integrity_sha256()` hashes it, `to_line()` writes payload + integrity_sha256. Every appended record carries `prev_sha256` = the previous line's integrity_sha256 (GENESIS_SHA256 = `"0"*64` for the first), and — this is the point — that link is **inside the hashed payload**, so it cannot be rewritten to conceal a deletion. `append_record()` forcibly overwrites whatever prev_sha256 the caller put on the dataclass, because the chain must reflect file order, not intent. `verify_line()` re-hashes one record; `verify_chain()` walks the ledger and returns False on a broken link, a reordering, a rewritten line, or an unchained line appearing after chaining began (a splice). It tolerates a legacy unchained prefix — the real ledger has one. Its docstring states the honest limit: deleting the legacy prefix, or truncating the whole file at its final line, is outside what an intra-file chain can prove; anchor the head externally if that ever matters.

**Current live state (verified 2026-07-30):** 12 records, all `experiment="backtest:panel.csv"`, all `gate_passed=false`; the first 11 are legacy/unchained and the 12th carries `prev_sha256=0f08774d...`. `summarize_manifest(load_manifest("research_ledger.jsonl"))` returns `{'total': 12, 'gate_passed': 0, 'gate_failed': 12, 'all_integrity_ok': True, 'chain_ok': True}`. Note the file shows as modified in `git status` — that is expected working-tree state, not corruption.

**How a run becomes a trial.** `cmd_backtest` (cli.py:776-857) does this on EVERY invocation: load the prior ledger, harvest each prior record's `metrics.per_period_sharpe` (finite entries only), append this run's per-period Sharpe, run `assess_edge(net_spreads, trial_sharpes, ...)` deflated by that whole lifetime set, then append a new record with `trials=len(trial_sharpes)`, and print `trial recorded in <path> (lifetime trials: N)`. In JSON mode the report gains additive `significance` and `ledger` keys. Before any of that, the panel must pass the input contract (`backtest.py:188` `_input_contract`): `filed_through` present, and attestation columns `universe_is_pit`, `return_is_total`, `delisting_return_included`, plus a fully-populated permanent identifier (`cik` / `security_id` / `permanent_id`). Failing any of these raises unless you pass `--allow-unverified-panel`, and the PASS/FAILED string is recorded verbatim in the record's `leakage_controls` field — so an exploratory run stays labeled as one forever.

**Why you must not delete ledger lines.** Deleting lines lowers the trial count, which lowers `expected_max_sharpe`, which raises DSR, which manufactures an 'EDGE' verdict out of pure selection bias. The code says so out loud at cli.py:776-780: 'Deleting the ledger to reset the count is exactly the fraud the SHA-256 chain in the manifest is designed to make visible.' Records are never mutated so that the history of what was actually tested — including the many honest negatives — stays preserved and verifiable. If you believe a line is wrong, append a corrective record; never edit or remove one.

**Why you must not silently add trials either.** The error runs both ways. Every accidental backtest against the real ledger inflates the lifetime trial count and raises the bar for every future run — it can bury a genuine edge. This is precisely why tests point `--ledger` at a scratch path, with the comment at tests/test_cli.py:340-343: 'without this the run appends a junk trial to the repo's real research_ledger.jsonl, deflating every future DSR.' **Imperative: any exploratory or automated backtest must pass `--ledger <scratch path>`; only a deliberate, recorded research trial writes to research_ledger.jsonl.**

**The forward-evidence side of the same machinery.** Frozen panels are the only backtest input that cannot possibly be overfit to the future, because they were written before it happened. `cmd_freeze` never overwrites an existing date, and it REFUSES to write a profile whose graded count falls below `config.min_letter_peers` (default 15) — that pattern is a data outage (an EDGAR blackout on freeze day), not a cross-section, and 'once a valid-looking parquet exists it is trusted downstream forever.' Panel columns: signal_date, ticker, cik, score, letter, percentile, coverage, graded, profile, config_fingerprint, universe_fingerprint, code_commit, schema_version. On the execution side, the paper trader's rules are pre-registered in a commit, not chosen at runtime: RULES_VERSION `v1-top10-equal-monthly`, TOP_N 10, MAX_WEIGHT 0.10, MAX_PANEL_AGE_DAYS 45 interlock, base URL hardcoded to `https://paper-api.alpaca.markets` so live keys cannot be used. 'Improvements come from changing the rules in a commit (auditable) and never from judgment calls inside the order path.' A full pre-registration record kind in research_manifest (declare the grid before running; mark results 'primary' only when the spec hash matches) is still deferred — see HANDOFF.md §10.

## Ecosystem-wide rules

Violating any of these has broken this system before.

- Do not bump SCHEMA_VERSION anywhere to make a shape change fit. It is a four-file change across two repos (Stock-Data/manifest.py:18, Stock-Vault/manifest.py:12, foundry.py:31, vault.py:31) and any consumer not updated hard-raises FoundryError/VaultError 'refusing to read'. Extend manifests additively via the `extra` dict instead.
- Do not write a data file into a dataset directory without publishing a manifest that lists it. FoundryDataSource._read_dataset_file and VaultDataSource._read_verified both raise on '<name> is not listed in <dir>/manifest.json' — an unmanifested file is unreadable by the entire ecosystem no matter that it exists on disk.
- Do not let line endings be translated in any manifested data path. sha256 covers exact bytes; Stock-Data/.gitattributes pins `*.jsonl text eol=lf` and Stock-Vault/.gitattributes pins `data/** -text`. Adding a new data file type without extending .gitattributes reproduces the 'hash mismatch on Windows checkout' failure.
- Do not register a CLI flag that a workflow passes only on the top-level parser. Workflows call subcommand-first (`stock-data snapshot-symbols --data-dir data`). Use the existing `shared = ArgumentParser(add_help=False)` + `parents=[shared]` pattern. The top-level-only variant silently killed every scheduled run in two repos ('DOA workflow').
- Do not run `stock-grader backtest` without `--ledger <scratch path>` unless you intend to permanently record a research trial. Every run appends to research_ledger.jsonl and deflates every future DSR (tests/test_cli.py:340-343 exists precisely because this happened).
- Do not delete, edit, reorder, or 'clean up' lines in research_ledger.jsonl. verify_chain() will report False, and lowering the trial count manufactures a false EDGE verdict. Append a corrective record instead.
- Do not let a NaN into ledger metrics or trial Sharpes. One non-finite value makes stdev — and therefore every subsequent DSR — NaN. Store None/JSON null; the guards are cli.py:824-837 and significance._finite_trial_sharpes.
- Do not commit anything derived from stockanalysis.com, Massive/Polygon, IBKR, Finnhub, SSGA, or FINRA into Stock-Data or Stock-Grader. Finnhub's terms cover DERIVED results, not just raw data. Public git history is forever; a force-push does not retract it, and the scheduled workflows auto-push on the next cron tick.
- Do not make Stock-Grader a private repository. paper-trader.yml checks it out with the default GITHUB_TOKEN, which cannot read other private repos, and the paper clock dies at checkout without a fine-grained PAT.
- The frozen-panel layout IS settled: the grader writes frozen_scores/<profile>/<date>.parquet and paper.py reads it profile-aware, with the flat layout kept as a deliberate legacy fallback for all_weather. Treat that fallback as load-bearing, not as dead code to tidy away.
- Do not remove a staleness gate to make a red workflow green. The gate runs BEFORE collection on purpose, and GitHub's scheduled-failure email to the owner IS the alerting system. Fix the clock, not the gate.
- Do not remove the heartbeat commit from daily-snapshot.yml. GitHub disables cron workflows after 60 days without commits; the always-commit heartbeat is what keeps the point-in-time archive alive across quiet stretches.
- Do not derive a staleness clock from file mtimes. Checkout mtimes describe the checkout, not the observation; every check in Stock-Vault/staleness.py derives its clock from an artifact filename or manifest content.
- Do not run PowerShell 5.1 with `2>$null` on a native command under strict mode (NativeCommandError turns exit 0 into a failure — use `cmd /c "... >nul 2>&1"`), and never pipe a secret to stdin (CRLF is appended and corrupts the stored secret; use `gh secret set NAME --body $value`).
- Do not treat a workflow_dispatch as done. Verify with `gh run list --repo <owner/repo> --limit 5` — a dispatched-but-dead run already burned one unrecoverable day of the PIT archive.
- Do not import code across repos, however small the helper. ECOSYSTEM.md Rule 1 is artifacts-not-imports; the established remedy (HANDOFF item 4) is to copy the helper into each repo.
- Do not lower Stock-Grader's coverage ratchet (fail_under = 55, declared in pyproject and in the CI flag) to get a change merged, and do not move a file into the 'hardened surface' lint/type list without making it actually clean — that list is blocking, the repo-wide pass is advisory.
- Do not skip `pip install -e ".[dev]"` before running Stock-Grader's suite. Two subprocess tests import the installed live tree and produce phantom failures against a stale install.
- Do not expect Stock-Vault's 45 tests to be caught by CI — it has no CI workflow. Run pytest locally before every commit to that repo.
- Do not wrap an existing documented JSON output in a new top-level envelope. Add keys additively (cli.py:859-865 is the reference pattern). This has been done wrong twice already.
