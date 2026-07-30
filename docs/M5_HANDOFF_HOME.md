# M5 home-computer handoff

Last updated: 2026-07-30 (America/New_York)

This document is the continuation record for M5, "Widen the frozen universe from
82 to 500-1000 names." It is intentionally operational: it records what is
already implemented, the exact artifacts and hashes that must survive the move
to the home computer, what has been proved, what is still incomplete, and the
commands that remain.

Do not call M5 complete until every acceptance item in
`docs/majors/M5-wide-universe.md` is genuinely satisfied. In particular, the
The measured N=250/500/1000 rollout is complete. Final Grader full-suite
validation, peer/sector diagnostics, and the Grader workflow dispatch remain.

## 1. Safety first on the home computer

The three repositories are:

| Repository | Local path | Visibility |
|---|---|---|
| Stock-Grader | `C:/Users/tforstrom/Desktop/Stock-Grader` | PUBLIC |
| Stock-Vault | `C:/Users/tforstrom/Desktop/Stock-Vault` | PRIVATE; never make public |
| Stock-Data | `C:/Users/tforstrom/Desktop/Stock-Data` | PUBLIC |

Before doing anything else:

```powershell
git -C C:/Users/tforstrom/Desktop/Stock-Grader pull
git -C C:/Users/tforstrom/Desktop/Stock-Vault pull
git -C C:/Users/tforstrom/Desktop/Stock-Data pull

git -C C:/Users/tforstrom/Desktop/Stock-Grader status --short
git -C C:/Users/tforstrom/Desktop/Stock-Vault status --short
git -C C:/Users/tforstrom/Desktop/Stock-Data status --short
```

If any tree contains unexpected changes, stop. Do not overwrite another
agent's work. In Stock-Grader especially, preserve the immutable universe files,
the append-only research ledger, and the staged SEC CDN-generation fix described
below.

Read these files in order before continuing:

1. `docs/MAJOR_IMPROVEMENTS.md`
2. `docs/majors/ORIENTATION.md`
3. `docs/majors/M5-wide-universe.md`
4. `docs/HANDOFF.md`
5. this file

## 2. Repository and branch state at handoff

### Stock-Grader

- Branch: `codex/m5-wide-universe`
- Main/base commit: `9881a36`
- Core M5 implementation commit: `c276a0a`
- SEC CDN fix, public artifacts, ledger, measurements, and handoff commit:
  `27b385d`.
- Agent-log/final handoff update: the commit immediately following `27b385d`.
- Remote after push: `origin/codex/m5-wide-universe`.

### Stock-Vault

- Branch: `codex/m5-wide-universe`
- M5 collector implementation commit: `4a607e1`
- Current branch HEAD after the verified collector dispatch: `200c6e4`
- Remote: `origin/codex/m5-wide-universe`
- Working tree was clean during the final independent audit.

The `200c6e4` commit is the normal data/heartbeat commit produced by the
successful collector workflow; the M5 code itself is in `4a607e1`.

### Stock-Data

- Branch: `codex/m5-wide-universe`
- M5 ecosystem documentation commit and HEAD: `01eddae`
- Remote: `origin/codex/m5-wide-universe`
- Working tree was clean during the final independent audit.

## 3. Licensing decision (do not reverse casually)

M5 uses licensing option B.

Massive/Polygon market-EOD data and derived works remain private in
Stock-Vault. The project owner did not provide a separate redistribution
license or a prior decision allowing a vendor-derived membership list to be
published. The M5 implementation therefore treats Massive's Section 5(c)
derived-work restriction conservatively:

- private median-dollar-volume values, ranks, rank order, and membership remain
  in Stock-Vault;
- Stock-Grader does **not** consume that private ranking to produce its public
  membership;
- the public membership is generated independently from SEC
  `dei:EntityPublicFloat`, a US-government public-domain fact under 17 USC 105;
- the legacy experiment identifier `liq1000_v1` is retained only because it is
  the pre-registered M5 identifier. It does not mean that the public file was
  selected by vendor-derived liquidity.

The public path is:

```text
Stock-Data verified symbol manifests
        + SEC bulk Companyfacts/EntityPublicFloat
        -> immutable alphabetic Stock-Grader universe file
        -> frozen_scores_wide/<profile>/<date>.parquet
        -> backtest
```

The private diagnostic path ends in Stock-Vault and does not feed the public
path under option B.

## 4. Completed implementation

### Stock-Vault

Implemented in `4a607e1`:

- `stock-vault universe-screen`, registered on the subcommand with
  `parents=[shared]`;
- pre-registered private spec at
  `config/universe_liquidity_screen_spec.json`;
- one-pass reading of each selected market-EOD day;
- strict numeric parsing for candidate observations;
- dot-to-dash symbol canonicalization;
- ETF, test-issue, exchange, SEC CIK, minimum-observation, and median-price
  filters;
- deterministic median-dollar-volume ranking and alphabetic public-safe test
  emitter;
- private deterministic `ranked.jsonl.gz` plus manifested hash/row count;
- a 100-day staleness clock based on the dated directory name, not checkout
  mtime;
- a pre-collection "due for rebuild" path that does not block the later
  self-healing collector step;
- Stock-Data checkout and conditional quarterly rebuild in
  `.github/workflows/collectors.yml`;
- README licensing and one-direction-DAG documentation.

### Stock-Grader

Implemented in `c276a0a`:

- public `config/universe_spec.json`;
- manifest-verified Stock-Data symbol-directory reader;
- SEC bulk Companyfacts ZIP support with sidecar provenance, same-day
  zero-network reuse, generation cleanup, missing-member fallback, and
  selective per-CIK reads;
- public-float universe generation with:
  - facts constrained by both measurement end and filed date;
  - listed-exchange, ETF, test-issue, and CIK rules;
  - one canonical SEC-dash ticker per CIK;
  - descending public-float rank with ticker-ascending ties;
  - alphabetic public membership output;
  - stage-specific universe IDs for N=250 and N=500;
  - a composite source hash covering the SEC ZIP SHA and both Stock-Data
    manifests;
- additive `UniverseSelection` provenance loading;
- future/stale universe selection guards;
- additive `universe_id` and `universe_spec_sha256` panel columns;
- multi-profile grading that builds the metric matrix once;
- normalized-matrix sharing across compatible profiles;
- `Fundamentals._clean_dated_frame` memoization with PIT-aware cache identity;
- one-pass/cached Vault market-EOD panels and a real Vault price provider;
- mixed-`universe_id` backtest refusal plus explicit waiver;
- additive `sector_neutral_key` with `business_model`, `sic2`, and `sic3`;
- preservation of the legacy default config fingerprint:
  `751441e6b469c806e7df459d3be71c0219e98364921160608243f791498540c7`;
- measurement helpers:
  - `scripts/generate_sec_float_universe.py`
  - `scripts/measure_wide_coverage.py`
  - `scripts/measure_peer_widening.py`;
- separate `frozen_scores_wide/` workflow root while retaining the original
  82-name forward clock;
- 300-minute workflow timeout, `df -h`, bulk facts, and no ticker sharding.

### SEC CDN HEAD/GET generation-race fix (staged after `c276a0a`)

Real generation exposed an SEC CDN race: HEAD advertised a newer/larger
generation while GET still returned the preceding valid representation. The
old code compared GET bytes to the stale-ahead HEAD length and raised.

The staged fix:

- adds `SECClient.get_bytes_with_headers()`;
- validates GET bytes against the GET representation's `Content-Length`;
- names and sidecars the archive using the GET representation's
  `Last-Modified`;
- logs, rather than corrupting provenance, when HEAD and GET describe different
  generations;
- retains compatibility with small injected clients.

The regression test
`test_download_validates_the_get_generation_when_head_is_ahead` failed against
the old implementation with:

```text
SECBulkFactsError: expected 327801, received 146
```

The new SEC-bulk focused set passed 14 tests.

### Stock-Data

`01eddae` documents the licensing-safe public path, artifact-only repository
boundary, and one-direction placement of the wide-universe output.

## 5. Exact generated artifacts

### Public Stock-Grader specification

Path:

```text
config/universe_spec.json
```

SHA-256:

```text
bd02dccb979fd57cfc059e608be2bfea7bb7e7f3dd25ce34c1364c71371ebe94
```

It parses as schema version `1.0` and contains every milestone key plus the
additive issuer-ticker and observation rules.

### Public Stock-Grader universe files

All three files:

- use selection as-of `2026-07-30`;
- carry spec SHA
  `bd02dccb979fd57cfc059e608be2bfea7bb7e7f3dd25ce34c1364c71371ebe94`;
- carry composite source SHA
  `21eae853c4505ef39498cb15a2e1b59a556801a58fd10b07cfbb16280f427069`;
- contain alphabetically sorted ticker-only member lines;
- contain zero member lines with digits;
- contain no CIK, float value, rank, or rank-order leak.

| File | Universe ID | Rows | Bytes | File SHA-256 |
|---|---:|---:|---:|---|
| `config/universe_liq250_2026-07-30.txt` | `liq1000_v1_stage250` | 250 | 1,285 | `b2005b1ecf890fe7d6c21943f7a4e682872af39d2e22a04210d6d2b94aebdc22` |
| `config/universe_liq500_2026-07-30.txt` | `liq1000_v1_stage500` | 500 | 2,350 | `da00b7d1b937b803f4d5ea3344edc14701e25f62bf15a361b02932147607bc0f` |
| `config/universe_liq1000_2026-07-30.txt` | `liq1000_v1` | 1,000 | 4,522 | `9d174b5b73b1f27d708d23951040850c0179b00fa8f6ed08c5dc55d63d67a409` |

These files are immutable experiment inputs. Do not overwrite them with a
different selection or silently change their headers. If a fresh checkout
does not reproduce these hashes, stop and investigate.

Quick verification:

```powershell
cd C:/Users/tforstrom/Desktop/Stock-Grader

Get-FileHash -Algorithm SHA256 config/universe_spec.json
Get-FileHash -Algorithm SHA256 config/universe_liq250_2026-07-30.txt
Get-FileHash -Algorithm SHA256 config/universe_liq500_2026-07-30.txt
Get-FileHash -Algorithm SHA256 config/universe_liq1000_2026-07-30.txt

python -c "import sys; sys.path.insert(0,'src'); from stock_grader.cli import _load_universe; print(len(_load_universe('config/universe_liq1000_2026-07-30.txt')))"
rg -n "^[^#].*[0-9]" config/universe_liq1000_2026-07-30.txt
```

Expected loader count: `1000`. Expected `rg` output: none.

### Private Stock-Vault liquidity diagnostic

Paths:

```text
data/universe_screen/2026-07-30/ranked.jsonl.gz
data/universe_screen/2026-07-30/manifest.json
```

Exact values:

| Property | Value |
|---|---|
| Universe ID | `liq1000_private_mdv_v1` |
| Rows | 1,000 |
| Ranked bytes | 31,665 |
| Ranked SHA-256 | `542bd725fa8f7acb1919202ba5797933428abd2b7eadcd1cfe54471676ab8583` |
| Manifest bytes | 943 |
| Manifest file SHA-256 | `ab332b182697be039f8b7dc9bac0597580bc6c5c65a18d7e1afebf41274bfc09` |
| Private spec SHA-256 | `8bbeb24585479d0f6c7dfacc320e8f6e56cdb43d306c55a1a668624e300ff48c` |
| Window | `2026-04-29..2026-07-29` |
| As-of | `2026-07-30` |
| `volume_is_fractional` | `true` |

The private spec SHA is deliberately different from the public spec SHA:
these are different licensed artifacts and different selection rules.

Do not copy the ranked private artifact, its membership, its values, or its
rank order into Stock-Grader or Stock-Data.

## 6. Workflow evidence

Stock-Vault collector dispatch:

- repository: `TylerJForstrom/Stock-Vault`
- branch: `codex/m5-wide-universe`
- run ID: `30569343825`
- conclusion: `success`
- duration reported by `gh run list`: 32 seconds
- URL:
  `https://github.com/TylerJForstrom/Stock-Vault/actions/runs/30569343825`
- pre-check, universe-screen step, and commit step were green;
- the run produced branch HEAD `200c6e4`.

Stock-Grader's M5 workflow has **not** yet been validly dispatched from the M5
branch. The visible successful Grader monthly-freeze run `30557005303` ran on
`main` before the M5 branch and does not satisfy M5 acceptance.

> **PRIMARY FILL - final Stock-Grader workflow evidence:**
>
> - Branch pushed at commit: `<COMMIT_SHA>`
> - Run ID: `<RUN_ID>`
> - Run URL: `<URL>`
> - Conclusion: `<SUCCESS_OR_FAILURE>`
> - Exact `gh run list` line: `<PASTE_LINE>`
> - Wide files written: `<COUNT_AND_PROFILE_LIST>`
> - Second-invocation result: `<EXIT_CODE_AND_NOTHING_WRITTEN_EVIDENCE>`

## 7. Test and performance evidence already obtained

### Stock-Grader

Baseline at `9881a36`:

```text
582 passed in 1504.65s (0:25:04)
```

Post-core implementation at `c276a0a`:

```text
621 passed in 807.02s (0:13:27)
```

An earlier pre-commit run of the same 621-test tree also passed:

```text
621 passed in 1074.51s (0:17:54)
```

Combined targeted regression set:

```text
190 passed in 1007.86s (0:16:47)
```

Ruff:

```text
All checks passed!
```

82-name cache-only performance proof:

| Version | Wall clock | Exit |
|---|---:|---:|
| old per-profile implementation | 1,148.819 s | 2 |
| shared multi-profile implementation | 200.401 s | 2 |

The new time is 17.4% of the old time (5.73x faster), below the milestone's
25% ceiling. Both runs attempted the same eleven profiles and refused the same
cache-starved profiles, so the timing comparison is like-for-like.

The staged CDN fix and generated artifacts landed after the `c276a0a` full
suite. They still require final full Grader validation.

> **PRIMARY FILL - final Stock-Grader validation:**
>
> - Installed command: `python -m pip install -e ".[dev]"`
> - Pre-final-commit pytest line: `<EXACT_LINE>`
> - Pre-final-commit Ruff line: `<EXACT_LINE>`
> - Final commit: `<COMMIT_SHA>`
> - Post-final-commit pytest line: `<EXACT_LINE>`
> - Post-final-commit Ruff line: `<EXACT_LINE>`
> - Final test count (must be at least 582): `<COUNT>`

### Stock-Vault

`python -m pytest -q` exited 0:

```text
........................................................................ [ 76%]
......................                                                   [100%]
```

This is 94 tests. The repository's pytest configuration suppresses a separate
`94 passed` summary.

`python -m ruff check .`:

```text
All checks passed!
```

### Stock-Data

`python -m pytest -q` exited 0:

```text
......................................                                   [100%]
```

This is 38 tests. The repository's pytest configuration suppresses a separate
`38 passed` summary.

`python -m ruff check .`:

```text
All checks passed!
```

## 8. Old-code regression proofs

The user required each behavior test to be shown failing against the old
implementation. The following failures were observed while restoring the
pre-M5 file/version, then the new file was restored:

| Behavior | Old-code proof |
|---|---|
| Multi-profile grading | Test collection failed with `ImportError` because `grade_universe_multi` did not exist; pytest exit 4. |
| Mixed-universe guard | The old `_validate_panel` did not raise; the regression test failed with "DID NOT RAISE"; pytest exit 1. |
| Vault market-EOD panel/cache | Two tests failed because the old adapter lacked `market_eod_panel` and `default_cache_dir`. |
| Foundry symbol directories | The test failed because old `FoundryDataSource` lacked `symbol_directory`. |
| SEC bulk facts | Old tree had no `stock_grader.data.sec_bulk`; import/collection failed; pytest exit 2. |
| Universe provenance loader | Two CLI tests failed because the old tree lacked `UniverseSelection` and `_load_universe_selection`. |
| Monthly wide workflow | The workflow regression failed because the old YAML lacked timeout 300 and the wide step. |
| Measurement scripts | Old tree lacked the measurement modules/scripts; import/collection failed. |
| Vault universe screen | Old Vault lacked `universe_screen`; import/collection failed. |
| Vault staleness | Old Vault lacked `check_universe_screen`. |
| Vault scheduled rebuild | The old collector workflow lacked the universe-screen step, so its workflow assertion failed. |
| Vault manifest row counts | The old `write_manifest` rejected `row_counts` with `TypeError`. |
| Vault CLI | The old CLI had no `universe-screen` subcommand. |
| SEC CDN split generation | Old code raised `SECBulkFactsError: expected 327801, received 146`; new focused set has 14 passing tests. |

If any new behavior is added while finishing the measured rollout, repeat the
same old-file failure proof before committing it. Do not treat "the old code
obviously lacked the function" as a substitute for the requested empirical
failure.

## 9. Research ledger

Exactly one record has been appended in the current Stock-Grader working tree:

- `experiment`: `universe:liq1000_v1`
- `verdict`: `PRE-REGISTERED`
- `trials`: `11`
- `metrics`: `{}`
- `code_commit`: `c276a0a`
- integrity SHA:
  `03dd81c33e0dc2f5b16560418d8676a5dfddfbf08ef8dbd5ceaa9ad50c15d529`

The full chain currently verifies:

```powershell
cd C:/Users/tforstrom/Desktop/Stock-Grader
python -c "import sys; sys.path.insert(0,'src'); from stock_grader.research_manifest import load_manifest, verify_chain; print(verify_chain(load_manifest('research_ledger.jsonl')))"
```

Expected:

```text
True
```

Never edit or remove an existing ledger line. Corrections are new appended
records only.

## 10. Discrepancies and adaptations that must be reported

These are not all bugs. They are places where the milestone prose, verified
ground truth, real data, licensing, or live services contradicted one another.

1. **The requested Jul-31 artifact was in the future.** Work ran on
   2026-07-30 and the available Stock-Data/SEC clock supported Jul-30. The
   immutable public files and workflow therefore use `2026-07-30`.

2. **Licensing option B breaks the originally described Vault-to-public
   membership arrow.** The private Vault screen is a diagnostic only. The
   public file is selected independently from SEC public float. This is the
   correct conservative licensing adaptation.

3. **The milestone alternates between ticker+CIK membership and ticker-only
   acceptance.** The existing loader and the explicit no-digit/no-CIK public
   test require ticker-only member lines. CIK remains available through the
   verified source manifests and is not emitted in membership.

4. **The literal digit-grep criterion is internally inconsistent.** Required
   header text such as `liq1000_v1`, `sha256`, the date, and row count contains
   digits. The meaningful verified condition is that no non-comment/member
   line contains a digit.

5. **SEC-only cannot honestly write all eleven profile panels.** Momentum and
   low-volatility require dense daily prices. SEC supplies sparse price
   evidence, so their defining-pillar gates refuse. The implementation does
   not weaken safety gates.

6. **The required second SEC-only invocation conflicts with the alarm
   policy.** After successful profiles already exist, only structurally
   refusing profiles remain pending. `cmd_freeze` returns 2 when every pending
   profile refuses. Therefore the literal "exactly 11 files, second invocation
   exits 0" criterion cannot be met by the specified SEC-only command without
   a dense licensed provider or an explicit acceptance amendment.

7. **The private archive contained zero-volume bars.** Step 3 said volume must
   be positive. The implementation treats a finite zero-volume bar as no
   liquidity observation, while absent, unparseable, non-finite, negative, or
   overflowing candidate numerics raise. This avoids fabricating liquidity
   while allowing the real archive to be screened.

8. **The measured private archive clock moved.** Ground-truth prose cited 501
   days through Jul-28; the live collector had advanced through Jul-29 when the
   screen was built.

9. **`--vault` means the Stock-Vault repository root.** The correct path is
   `C:/Users/tforstrom/Desktop/Stock-Vault`, not its `data/` directory.

10. **Grader and Vault argparse structures differ.** Vault genuinely uses
    `parents=[shared]`. Grader's established parser registers shared flags
    directly on each subcommand through its `common(...)` helper. Flags must
    still appear after the Grader subcommand.

11. **SEC's CDN can race HEAD against GET.** Live generation found HEAD
    describing a newer generation while GET served the preceding valid ZIP.
    The staged fix validates and records the GET representation instead of
    rejecting or mislabelling it.

12. **The default config fingerprint initially drifted when the new sector key
    was added.** The final implementation omits the default
    `business_model` key from the legacy manifest and fingerprints only
    non-default `sic2`/`sic3`, preserving the existing frozen-panel contract.

13. **The old Vault series refactor initially dropped the `symbol` column.**
    The final `_series_from_panel` preserves the exact legacy frame shape.

14. **One CIK may map to multiple traded tickers.** `EntityPublicFloat` is
    issuer-level, so the final public rule retains exactly one deterministic
    ticker per CIK; otherwise one issuer could consume several ranked slots.

## 11. Current acceptance status

| Acceptance item | Status at handoff |
|---|---|
| Grader full suite before/after core commit | Met: 582 baseline, 621 post-core |
| Final Grader suite after CDN/artifacts/docs | **Pending** |
| Vault suite and fixture screen | Met |
| Public spec | Met |
| Public 1,000-name file | Generated and hash-verified; **not yet final-committed at handoff** |
| Multi-profile equality test | Met |
| 82-name speedup <=25% | Met: 17.4% |
| Clean dated-frame cache equality | Met |
| SEC bulk regression set | Met; final CDN addition has 14 focused passes |
| Vault panel one-read/exact-series test | Met |
| Mixed-universe guard | Met |
| SEC-only exactly 11 + second exit 0 | **Blocked by documented price/alarm contradiction** |
| N=250/500/1000 timing table | Met; measured rows are below and in `docs/UNIVERSE.md` |
| N=1000 under 100 minutes | Met: 1,253.896 seconds (20.90 minutes) |
| Grader workflow branch dispatch green | **Pending** |
| Exactly one pre-registration ledger record + valid chain | Met in working tree; final commit pending |
| Coverage/graded and retuning measurements documented | Partial: same-date 82-to-1,000 coverage and unchanged 0.35 gate are documented; peer/sector measurements remain |
| Ecosystem and Vault docs | Met |
| `docs/REVIEW_FEEDBACK.md` M5 commit-hash line | **Pending** |

M5 is not complete at handoff.

## 12. Completed stage results

These are real low-overhead measurements from
`scripts/measure_wide_freeze.py`, not estimates. All three commands used
SEC-only prices and the verified local bulk archive. The residual column is
the total across eleven profiles with the mean in parentheses.

| N | Snapshot-build seconds | `build_metric_matrix` seconds | Per-profile residual seconds | Total wall clock | Peak RSS | Disk used | Graded fraction | Unresolved CIK count |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 250 | 154.497 | 56.609 | 166.896 total (15.172 mean) | 381.875 s | 534,245,376 B | 165,042 B | 95.467% | 0 |
| 500 | 185.465 | 70.709 | 299.525 total (27.230 mean) | 558.708 s | 842,231,808 B | 232,870 B | 94.867% | 0 |
| 1,000 | 421.522 | 138.020 | 690.361 total (62.760 mean) | 1,253.896 s | 1,456,394,240 B | 391,314 B | 94.078% | 0 |

Staged-run provenance:

- Python: the active Python 3.12 environment used by all prior Grader tests.
- Cache state: verified local SEC bulk ZIP plus previously loaded SEC insider
  price index covering 4,252 tickers; no per-issuer network sweep.
- SEC bulk representation: `companyfacts_2026-07-28.zip`, 1,390,705,602
  bytes, 20,102 members, full ZIP CRC clean, SHA-256
  `5d69b5a1fd4c76203733793b0e417fea7036588341618ae264be934d2343f5fb`.
- Output roots: `C:/tmp/m5-stage250b`, `C:/tmp/m5-stage500`, and
  `C:/tmp/m5-stage1000b`.
- Timing JSON: `C:/tmp/m5-stage250b-timing.json`,
  `C:/tmp/m5-stage500-timing.json`, and
  `C:/tmp/m5-stage1000b-timing.json`.
- Harness: `scripts/measure_wide_freeze.py`.
- N=1,000 under 100 minutes: **yes**, 20.90 minutes.
- Every stage wrote nine panels and returned zero; `low_volatility` and
  `momentum` were explicitly refused because SEC-only prices lack their dense
  daily histories.

## 13. Remaining work, in order

The measured work described in Steps 4-7 below is now complete on the source
machine. Those steps are retained as reproducibility instructions only; the
home agent should verify the committed table and timing JSON summary, then
continue at Step 8 unless a rerun is specifically needed.

### Step 1: preserve and verify the continuation branch

On the source machine, the primary agent should run the final pre-commit tests,
commit the staged CDN fix/public artifacts/ledger/handoff, and push the branch.
On the home computer, pull that exact branch and verify hashes before running
anything expensive.

```powershell
cd C:/Users/tforstrom/Desktop/Stock-Grader
git switch codex/m5-wide-universe
git pull
git status --short
git log --oneline --decorate -8
```

Do not regenerate a dated artifact merely because it already exists. Matching
bytes are the expected result; different bytes are a provenance incident.

### Step 2: install and run pre-commit Grader validation

```powershell
cd C:/Users/tforstrom/Desktop/Stock-Grader
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

Record the exact final summary lines. The suite must have at least 582 tests.
Run it before and after each remaining Grader commit.

### Step 3: verify the generated public artifacts

```powershell
cd C:/Users/tforstrom/Desktop/Stock-Grader

python -c "import json,hashlib,pathlib; p=pathlib.Path('config/universe_spec.json'); d=json.loads(p.read_text()); print(d['schema_version'], hashlib.sha256(p.read_bytes()).hexdigest())"
python -c "import sys; sys.path.insert(0,'src'); from stock_grader.cli import _load_universe; print(len(_load_universe('config/universe_liq250_2026-07-30.txt')), len(_load_universe('config/universe_liq500_2026-07-30.txt')), len(_load_universe('config/universe_liq1000_2026-07-30.txt')))"
rg -n "^[^#].*[0-9]" config/universe_liq250_2026-07-30.txt config/universe_liq500_2026-07-30.txt config/universe_liq1000_2026-07-30.txt
```

Expected:

```text
1.0 bd02dccb979fd57cfc059e608be2bfea7bb7e7f3dd25ce34c1364c71371ebe94
250 500 1000
```

The `rg` command must produce no member-line matches.

### Step 4: run the measured N=250 stage

Use a fresh scratch output root. Do not write stage panels into production
`frozen_scores_wide/`.

Base command:

```powershell
stock-grader freeze `
  --all-profiles `
  --universe config/universe_liq250_2026-07-30.txt `
  --out C:/tmp/m5-stage-250 `
  --bulk-facts auto `
  --price-provider sec `
  --vault C:/Users/tforstrom/Desktop/Stock-Vault `
  --asof 2026-07-30
```

The completed measurement used SEC-only prices, matching the public workflow,
and therefore recorded the two honest structural refusals rather than
weakening gates.

Instrument and record all eight required table columns. The current CLI does
not print every internal timing or peak RSS, so use the committed
`scripts/measure_wide_freeze.py` harness. Do not commit temporary caches or
scratch panels.

### Step 5: review N=250 before proceeding

- Confirm output universe ID is `liq1000_v1_stage250`.
- Confirm panel spec SHA equals the public spec SHA.
- Record graded fraction and unresolved CIK count.
- Profile and fix any superlinear residual before N=500.
- Publish the N=250 measurement row in `docs/UNIVERSE.md` before enabling the
  next stage.

### Step 6: run and review N=500

```powershell
stock-grader freeze `
  --all-profiles `
  --universe config/universe_liq500_2026-07-30.txt `
  --out C:/tmp/m5-stage-500 `
  --bulk-facts auto `
  --price-provider sec `
  --vault C:/Users/tforstrom/Desktop/Stock-Vault `
  --asof 2026-07-30
```

Repeat every N=250 check and publish the N=500 row before N=1000.

### Step 7: run and review N=1000

```powershell
stock-grader freeze `
  --all-profiles `
  --universe config/universe_liq1000_2026-07-30.txt `
  --out C:/tmp/m5-stage-1000 `
  --bulk-facts auto `
  --price-provider sec `
  --vault C:/Users/tforstrom/Desktop/Stock-Vault `
  --asof 2026-07-30
```

Do not enable the workflow until the measured total is below 100 minutes on
the home machine, as required by the milestone. If the residual is
superlinear, profile and fix it first.

### Step 8: measure coverage and peer widening

After a real wide all-weather panel exists:

```powershell
python scripts/measure_wide_coverage.py `
  frozen_scores/all_weather/2026-07-30.parquet `
  C:/tmp/m5-stage-1000/all_weather/2026-07-30.parquet `
  --output C:/tmp/m5-coverage.json
```

Export the real loaded wide snapshot metadata with the columns required by
`scripts/measure_peer_widening.py`, then run:

```powershell
python scripts/measure_peer_widening.py `
  C:/tmp/m5-wide-snapshot-metadata.parquet `
  --sample-size 50 `
  --seed 0 `
  --minimum 8 `
  --maximum 30 `
  --size-band-multiple 5.0 `
  --output C:/tmp/m5-peers.json
```

Measure and document:

- N=82 versus N=1000 coverage distribution and graded fraction;
- peer-count/size-band behavior;
- `business_model` versus `sic2`/`sic3` grouping behavior;
- whether the 15-name floor remains appropriate;
- whether `MIN_COVERAGE_TO_GRADE = 0.35` remains appropriate.

Every production decision must cite the measured value or explicitly say
"left unchanged, measured X."

### Step 9: update documentation and the Agent log

Update:

- the stage table and measurement decisions in `docs/UNIVERSE.md`;
- limitations if measurements expose a new caveat;
- one M5 line under `docs/REVIEW_FEEDBACK.md`'s Agent log with the final
  Stock-Grader, Stock-Vault, and Stock-Data implementation commit hashes.

Do not add a second pre-registration ledger record.

### Step 10: final pre-commit and post-commit Grader suites

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

Commit only after the pre-commit suite is green. Then run the same install,
full suite, and Ruff again at the commit. Preserve exact summary lines in the
placeholders above.

### Step 11: push and dispatch the Grader workflow

Only after the public artifact and workflow are committed:

```powershell
git push -u origin codex/m5-wide-universe

gh workflow run monthly-freeze.yml `
  --repo TylerJForstrom/Stock-Grader `
  --ref codex/m5-wide-universe

gh run list --repo TylerJForstrom/Stock-Grader --limit 5
```

Inspect the run until it is terminal. A dispatch request is not evidence; only
a green completed run is.

Be explicit about the known SEC-only profile refusal contradiction. Do not
claim the exact-eleven/second-exit-zero criterion is met unless a dense,
licensed source or an approved criterion change resolves it.

### Step 12: final repository audit

```powershell
git -C C:/Users/tforstrom/Desktop/Stock-Grader status --short
git -C C:/Users/tforstrom/Desktop/Stock-Vault status --short
git -C C:/Users/tforstrom/Desktop/Stock-Data status --short

git -C C:/Users/tforstrom/Desktop/Stock-Grader log --oneline --decorate -8
git -C C:/Users/tforstrom/Desktop/Stock-Vault log --oneline --decorate -8
git -C C:/Users/tforstrom/Desktop/Stock-Data log --oneline --decorate -8
```

Verify the ledger chain one final time and report every acceptance item as met
or not met with evidence. If the exact-eleven contradiction remains, M5
remains incomplete.

## 14. Cautions and known failure classes

- Stock-Vault is private. Never change its visibility or copy its restricted
  data into a public repository.
- `research_ledger.jsonl` is append-only and hash-chained. Never edit or delete
  an existing line.
- Universe and panel schemas are additive only.
- Never overwrite a dated universe file or frozen panel with different bytes.
- Do not pool narrow and wide panels or different universe IDs in one
  backtest.
- Never fail open on absent, malformed, non-finite, or overflowing feed
  numerics.
- Grader flags belong after the subcommand. Vault flags are registered through
  `parents=[shared]`.
- Use the Stock-Vault repository root for `--vault`.
- Do not weaken defining-pillar or minimum-peer gates to manufacture eleven
  panels.
- Do not shard by ticker. Scoring is cross-sectional and each ticker shard
  would use a different peer set.
- The public workflow cannot read Stock-Vault.
- Do not use GitHub Actions cache for the daily SEC bulk ZIP.
- After changing a workflow, dispatch that exact branch and verify it green.
- The full Grader suite is long; let it finish and preserve the exact output.
- Existing unrelated working-tree changes belong to the user or another
  agent. Stop rather than overwrite them.

## 15. Ready-to-paste continuation prompt

```text
Continue M5 on my home Windows computer.

Repositories:
  C:/Users/tforstrom/Desktop/Stock-Grader   PUBLIC
  C:/Users/tforstrom/Desktop/Stock-Vault    PRIVATE; never make public
  C:/Users/tforstrom/Desktop/Stock-Data     PUBLIC

First run git pull and git status in all three repos. If any tree has
unexpected changes, stop and tell me. Work only on codex/m5-wide-universe; do
not push to main.

Read, in order:
  Stock-Grader/docs/MAJOR_IMPROVEMENTS.md
  Stock-Grader/docs/majors/ORIENTATION.md
  Stock-Grader/docs/majors/M5-wide-universe.md
  Stock-Grader/docs/HANDOFF.md
  Stock-Grader/docs/M5_HANDOFF_HOME.md

Treat M5_HANDOFF_HOME.md as the exact continuation record. Verify its branch
commits and all public/private artifact hashes before running anything
expensive. Preserve the committed SEC HEAD/GET generation-race fix, the three
immutable public universe files, and the single append-only PRE-REGISTERED
ledger record.

Finish the remaining work in the handoff's order:
  1. verify the committed N=250/500/1000 table and public artifacts;
  2. run the remaining peer and sector-key measurements (same-date coverage
     and the unchanged 0.35 threshold are already documented);
  3. run the final Grader full suite and Ruff and preserve exact summaries;
  4. dispatch monthly-freeze.yml on the branch and verify
     the completed run with gh run list;
  5. perform the final three-repo status and acceptance audit.

Use real data only. Do not weaken safety gates, edit prior ledger lines,
overwrite immutable artifacts, pool universe IDs, leak Vault-derived data into
public repos, or shard scoring by ticker.

The milestone contains a known unresolved contradiction: the specified
SEC-only command cannot honestly write exactly eleven panels, and its second
invocation returns 2 when only structurally refusing profiles remain. Do not
claim M5 complete unless a dense licensed source or an explicit acceptance
amendment resolves that criterion. Report the contradiction precisely.

When done, report every M5 acceptance criterion as met/not met with evidence,
the exact pytest/Ruff summary lines for all touched repos, all discrepancies,
and every remaining blocker. Do not report M5 complete unless every criterion
is genuinely met.
```
