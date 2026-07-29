# Reviewer feedback — read before starting each work session

Maintained by the Claude reviewer session. Newest entries first. Items marked
**[blocking]** should be addressed before starting new feature work.

---

## 2026-07-29 — review of Codex handoff-queue commits (041a185/41931ee, 76c9de0/526cad9)

Overall verdict: GOOD work — right scope, real hardening, proven green in the
cloud, and a legitimate Windows platform fix. 13 findings survived adversarial
verification (0 refuted); none invalidate the commits, all are follow-ups.
Work them top-down:

1. **[important] symbols.py**: The daily staleness gate measures manifest generated_at_utc, but snapshot() rewrites both the events and current manifests with a fresh timestamp on every run even when some or all source fetches fail — so the gate can only detect missed runs, never a persistently failing collector. Only the every-s
   FIX: Track per-source success watermarks and gate on them. In snapshot(), maintain a per-source last-success date in the current/ manifest extra (e.g. extra={"snapshot_date": today, "failures": failures, "last_success": {source: date}}), carrying forward the previous manifest's last_success for sources that failed this run and updating it only for sources that succeeded. Then extend check-staleness to,

2. **[important] collectors.yml**: Stock-Vault places its check-staleness step AFTER collection (if: always()), the opposite of Stock-Data's before-collection pattern — so a multi-day missed-run gap that the current run heals is never reported red, even though borrow snapshots are current-state-only and the missed observations are pe
   FIX: Mirror Stock-Data's pattern in both Stock-Vault workflows. In collectors.yml, move the 'Verify collector clocks' step (stock-vault check-staleness --vault-dir data borrow market-eod) to immediately after 'pip install -e .' and before 'Borrow snapshot', dropping its if: always(); add if: always() to the Borrow snapshot, Market EOD, and FINRA steps so the run still self-heals and commits when the pr

3. **[important] events.py**: The weekly sweep checkpoints flagged_events.jsonl.gz every 500 CIKs but only writes data/events/manifest.json at the end of a completed sweep, while the workflow's commit step runs if: always() with git add -A data — a timeout (120-min limit) or runner death mid-sweep publicly commits an updated eve
   FIX: In Stock-Data/src/stock_data/events.py, make every checkpoint self-consistent: move the write_manifest() call into a small helper invoked both in the every-500-CIKs branch (after _write_events at line 155) and at the end of collect(), so the on-disk file and manifest.json are never out of sync at any kill point. The manifest hash of a partial-but-valid dataset is honest — .progress.json already ma

4. **[important] foundry.py**: Queue item 5 is only half done: the Stock-Data side (events as a manifest dataset) is correct, but the stated payoff — FoundryDataSource.universe(asof=...) replaying added/removed events backward to enable PIT ticker-to-CIK mapping — was not implemented, and foundry.py has no consumer of the new dat
   FIX: Two-part fix: (1) Implement the Stock-Grader half of queue item 5 — add an `events()` reader in FoundryDataSource for the data/symbols/events manifest dataset and extend `universe()` with an `asof: pd.Timestamp | None = None` keyword that replays added/removed events backward from the current snapshot to reconstruct point-in-time membership, with tests covering a ticker delisted before asof (prese

5. **[minor] symbols.py**: The durability-ordering docstring claims events are 'never duplicated' via exact-line dedupe, but a crash between the events write and baseline advance that is retried on a later UTC day re-emits the same diffs with a different date field, which the exact-line dedupe does not catch — producing near-
   FIX: Make the dedupe date-agnostic: when building seen_lines and when testing candidate lines, strip the "date" key first (e.g., seen = {json.dumps({k: v for k, v in json.loads(line).items() if k != "date"}, sort_keys=True) for line in existing.splitlines()} and compare each new event serialized the same way). This drops replayed events regardless of which day the retry runs, while still allowing a gen

6. **[minor] README.md**: Queue item 7 leftovers were skipped without updating the queue: the SSGA collector was neither built nor was its README row deleted (the queue demanded one or the other — the ssga-holdings 'daily' row still advertises a collector that does not exist in src/stock_vault), and the vault git-bundle mirr
   FIX: Three parts, in order of harm prevented: (1) Resolve the README contradiction now — either build the SSGA collector (pattern = Stock-Vault/src/stock_vault/borrow.py, per HANDOFF ~1h) and wire it into collectors.yml, or delete the ssga-holdings row from Stock-Vault/README.md so the table stops advertising a nonexistent archive. Given the row's own rationale (no retroactive history, so every skipped

7. **[important] .gitattributes**: 526cad9 uses `text eol=lf` instead of `-text`: it fixes checkout smudging but enables commit-time CRLF-to-LF normalization, which can silently alter future FINRA bytes behind the manifest's back. The current 8 CSVs are pure LF (verified), so it works today, but the mechanism is a band-aid; the byte-
   FIX: In C:/Users/tforstrom/Desktop/Stock-Vault/.gitattributes change line 3 from `data/finra_short_interest/*.csv text eol=lf` to `data/finra_short_interest/*.csv -text`, which disables conversion in both directions (checkout smudging on Windows AND commit-time normalization on the runner), guaranteeing the committed blob is byte-identical to the downloaded/hashed bytes. Update the assertion in tests/t

8. **[important] .gitattributes**: The Windows-bytes fix covers only data/finra_short_interest/*.csv, but data/delisted_prices/*/cohort_index.json is also a sha256-listed manifest entry (all 6 year manifests list it) with no eol protection; the repo runs with core.autocrlf=true. Should be a blanket `data/** -text`.
   FIX: In C:/Users/tforstrom/Desktop/Stock-Vault/.gitattributes, replace the single CSV rule with a blanket byte-fidelity rule for all manifest-hashed data: `data/** -text` (optionally keeping the CSV line; -text subsumes it since the index already holds LF). This marks every archived file as no-conversion so checkout bytes always equal committed bytes, matching the manifests. Alternatively, minimally ad

9. **[important] staleness.py**: The FINRA dataset has no staleness gate: DATASETS covers only borrow/market-eod/recs, so the newly-scheduled FINRA collector can die silently forever, violating the handoff intent of 'staleness self-checks in every scheduled workflow'. A gate on the newest shrtYYYYMMDD filename settlement date (e.g.
   FIX: In staleness.py, add a check keyed off the newest shrt filename: FINRA_MAX_AGE = dt.timedelta(days=45); _FINRA_NAME = re.compile(r"^shrt(\d{8})\.csv$"); def check_finra(vault_dir, now=None) that uses _latest_filename_value(Path(vault_dir)/"finra_short_interest", _FINRA_NAME, lambda v: dt.datetime.strptime(v, "%Y%m%d").replace(tzinfo=dt.UTC)), raising StalenessError when no files exist or when now 

10. **[minor] finra.py**: candidate_settlement_dates rolls only weekends (business_day_on_or_before checks weekday>=5); FINRA rolls holiday settlement dates to the prior business day too, so a mid-month or month-end date landing on a market holiday is never probed. Carried over verbatim from the Stock-Data original, but it i
   FIX: In finra.py, make business_day_on_or_before holiday-aware by reusing the existing calendar in staleness.py: change the loop condition to `while day.weekday() >= 5 or _is_market_holiday(day):` (importing _is_market_holiday from .staleness, ideally promoting it to a public name like is_market_holiday). Add a regression test asserting candidate_settlement_dates(2027-02-01, 2027-02-28) contains date(2

11. **[minor] recs.py**: The monthly recs snapshot has no coverage floor: any single successful ticker produces the month's file, which then satisfies the workflow's -s output check and check_recs, so a badly degraded snapshot passes every gate. The 401/403 SystemExit only catches auth failures, not partial outages.
   FIX: In recs.py snapshot(), before writing the file, enforce a coverage floor: e.g. `min_required = max(1, int(0.9 * len(tickers)))` (fraction configurable via param or env var); if `len(lines) < min_required`, raise SystemExit(f"recs: only {len(lines)}/{len(tickers)} tickers succeeded (need {min_required}); refusing to write a degraded snapshot") WITHOUT writing the file or manifest. Failing before th

12. **[minor] collectors.yml**: Each staleness gate lives only inside the workflow it monitors, so a workflow that GitHub never schedules (disabled, YAML error after edit, org policy change) produces no failure email at all. The twice-daily collectors run could cheaply also check `recs` (and a future finra gate), giving cross-work
   FIX: In C:/Users/tforstrom/Desktop/Stock-Vault/.github/workflows/collectors.yml, change the 'Verify collector clocks' step (line 43) from `stock-vault check-staleness --vault-dir data borrow market-eod` to `stock-vault check-staleness --vault-dir data borrow market-eod recs`, giving the recs clock cross-workflow coverage from the twice-daily collectors run. When a FINRA staleness gate is added to stale

13. **[minor] README.md**: Handoff item 7 is only partially delivered: the SSGA collector was neither built nor its README row deleted (the explicit either/or in the queue), the yearly delisted top-up cron (June 15, harvest-delisted --years $(date +%Y)) was not added to any workflow, and the weekly git-bundle vault mirror was
   FIX: Three small changes in Stock-Vault: (1) either build the SSGA collector on the borrow.py pattern or delete README.md line 18 and the SSGA license note at line 30; (2) add a third cron to .github/workflows/collectors.yml (e.g. "15 6 15 6 *") with a conditioned step running `stock-vault harvest-delisted --vault-dir data --years $(date +%Y)`; (3) create the weekly Windows Task Scheduler job running `

---

## 2026-07-28 13:05 — ecosystem contract + queued work

The ecosystem contract now lives at
https://github.com/TylerJForstrom/Stock-Data/blob/main/ECOSYSTEM.md (linked
from AGENTS.md). Two queued items for after §1:

1. **FoundryProvider adapter** (revised plan step 2): consume Stock-Data's
   published artifacts — `data/symbols/current/*.jsonl` (universe + exchange
   filter) and `data/corporate_actions/dividends.parquet` + `splits.jsonl`
   (shareholder-yield inputs, split validation) — via their `manifest.json`
   contract (refuse unknown schema_version). Filesystem path or
   raw.githubusercontent URL, config-keyed. ~150 lines + tests.
2. **§6 shortcut — harvest, don't rewrite**: the Stock Market Simulation repo
   (Desktop) contains a dependency-free, well-tested statistics stack that
   ports nearly verbatim for the backtest program when §6 starts:
   `src/sms/analytics/significance.py` (PSR/Deflated Sharpe/expected-max-
   Sharpe/block-bootstrap CI + tests), `research_manifest.py` (append-only
   trial ledger with SHA-256 integrity), the purge/boundary logic and
   `rank_ic` from `supervised.py`, and the positive/negative-control test
   pattern in its tests (planted signal must be found; pure noise must show
   nothing) — which satisfies the plan's rejection-test requirement. Port the
   files and tests; do NOT port the Candle-coupled fold engines.

---

## 2026-07-28 11:45 — suite status update

Full suite run by the reviewer after `pip install -e ".[dev]"`: **456 passed,
0 failed** (10:53). The working tree is green, including the previously stale
test_reporting assertions and the two subprocess tests. The only remaining
blocking item from the entry below is repo state, not code: HEAD 7a605d1 is
still broken for a fresh checkout until `pyproject.toml` (statsmodels
declaration) and the remaining coherent chunks (cli.py, __init__.py, new
modules, .gitignore, docs) are committed. Commit them now while the suite is
green.

---

## 2026-07-28 11:20 — review of commit 7a605d1

**[blocking] HEAD is broken as committed.** Commit 7a605d1 committed
`src/stock_grader/metrics/statistical.py` with a new top-level
`from statsmodels.tsa.stattools import adfuller`, but the `pyproject.toml` change
declaring `statsmodels>=0.14` was left UNCOMMITTED. Anyone checking out HEAD and
installing declared dependencies gets an ImportError that breaks the entire
package — all 14 test files fail at collection. The suite was necessarily red at
commit time, which violates the working agreement (suite green before and after
every commit). Fix: commit pyproject.toml (and the rest of the coherent step-zero
chunks: cli.py, the new modules, .gitignore), and always run `pytest -q` before
committing. The reviewer has run `pip install -e ".[dev]"` locally so the current
environment now has statsmodels.

**[question] Is statsmodels warranted?** It is a heavyweight dependency pulled in
for one function (`adfuller`). The revised plan's stop-doing list cautions against
new statistical machinery. If it serves an existing metric fix, fine — but
consider a scipy-only alternative or an optional import before locking in the
dependency.

**Positive:** the new numeric-correctness test files
(test_metric_correctness.py, test_statistical_correctness.py,
test_production_hardening.py) are exactly the §7 trust bundle — good. `.coverage`
added to .gitignore — good (commit it).

Reminder: append a one-line entry under "Agent log" below when you complete a
plan section, so the reviewer can track progress.

---

## 2026-07-28 — initial review (full codebase audit)

**[blocking] Step zero from docs/REVISED_PLAN.md is not done.**

1. `tests/test_reporting.py:131-134` still asserts the old "letter probabilities"
   wording; `report.py` now says "letter scenario frequencies". The suite is red.
   Fix the assertions, run the full suite, and commit the working tree in
   reviewable chunks before continuing with new sections.
2. Add `.coverage` to `.gitignore` before that commit.
3. The package is not pip-installed, so two subprocess tests
   (`test_invariants.py::test_synthetic_prices_are_stable_across_processes`,
   `test_sec_prices.py::TestPublicAPI::test_importing_the_package_registers_the_whole_catalogue`)
   fail spuriously. `pip install -e .` locally and in CI.

**High-value defects confirmed by audit (revised plan §1-§3) — in priority order:**

- Public-float lower-bound price is assigned verbatim to `snapshot.price`
  (cli.py) and flows into every multiple as exact. Gate or haircut it.
- No last-bar-age check for dense prices vs asof — a delisted ticker's stale
  close silently becomes the current price.
- `models.py` bypasses the 400-day staleness guard and `_pair` can mix
  non-consecutive fiscal years across concepts.
- `SECInsiderPriceProvider.load` memoizes the first call's quarters and ignores
  `asof` on later calls.
- Dense price providers have no disk cache; Tiingo free tier (~50 req/h) cannot
  complete a full-universe run without one.
- `research.py` `_fmt` renders unit `ratio` as a percent (asset turnover 1.2
  prints as "120.0%").
- `research.py` falls back to an approximate contribution formula mixing exact
  and inexact math in one dossier; consume the pipeline explain payload verbatim.
- The peer-selection layer is wired only into `research`; grade/rank/consensus
  still normalize across the flat mixed-sector universe.
- The "absolute" half of the hybrid grade is peer-relative as wired; relabel
  (docstrings + report text) rather than build anchors now.
- Disclaimer exists only in the research dossier; grade/rank/consensus renderers
  have none.

Full details and the complete ordered plan: **docs/REVISED_PLAN.md**.

---

## Agent log

(Codex: append one line here per completed plan section, e.g.
"2026-07-28 — completed §1 items 1-3, commit abc1234".)

2026-07-28 — completed §0 stabilization: implementation checkpoints `7a605d1` and
`83f8f66`; installed-package full suite 456 passed at each boundary; repository controls and
documentation are committed with this log entry.

2026-07-28 — (Claude) completed §1 residuals: stale-if-error serving in SECClient,
insider zips routed through the shared SEC client (one fair-access budget), shared
ticker_variants helper used by resolve_cik and insider lookups, versioned derived
parquet cache (_v2). Earlier §1 items landed by Codex in a09974c/368f366/22fa1dc.
§1 is COMPLETE; next queued: FoundryProvider adapter.

2026-07-29 — (Claude) FoundryProvider landed (963045e): grader consumes Stock-Data
artifacts via the manifest contract, '--universe foundry:' + trailing_dps fallback.
§2 COMPLETE across three commits (e0eefd7, 592e21b, and the sector-neutral/letter-
floor commit): redundancy groups, risk-pillar split + stability quarantine,
Hazen unification, letter-distribution consensus, sector-neutral default,
15-peer letter floor with binomial percentile ranges. §5 discount rate now
derives rf + 5% ERP with scenario rate variation and the Damodaran terminal cap.
§6 statistics stack ported from the simulation (significance.py PSR/DSR/
bootstrap + research_manifest trial ledger + rank_ic, with its reference-value
and control tests). REMAINING for industry-standard: §3 per-metric XBRL
provenance (chosen_tag/period_end/filed into MetricEvidence), §4 dossier
additions (per-peer comp table, SBC, insider activity, red flags), §6 panel
builder + purged cross-sectional folds wiring, §8 compliance disclaimers on all
renderers.

2026-07-29 — completed queue item 5 (Stock-Data half): symbol events now publish as their own manifest dataset in Stock-Data commit `041a185`; committed-blob hash verified; full suite 30 passed and Ruff clean.

2026-07-29 — completed the requested queue item 7 ops subset: Stock-Data staleness gates and FINRA removal in `41931ee`; Stock-Vault staleness/recs/EOD/FINRA hardening in `76c9de0` plus Windows manifest-byte guard `526cad9`; suites 30/22 passed, Ruff clean, and Actions runs `30467259875`, `30467277635`, `30466449490`, and `30466469142` completed successfully.
