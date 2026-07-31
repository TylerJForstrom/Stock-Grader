# M5 home-computer handoff

Last updated: 2026-07-31 (America/New_York)

This document is the completion and continuation record for M5, "Widen the
frozen universe from 82 to 500-1000 names." It records the implementation,
immutable artifacts and hashes, verification evidence, owner-approved acceptance
amendment, and the separately deferred dense-price follow-up.

M5 is complete under the owner's explicit 2026-07-31 amendment in
`docs/majors/M5-wide-universe.md`: nine valid public SEC-only profiles are the
production set, `momentum` and `low_volatility` are deferred to a licensed
point-in-time dense-price milestone, and the historical per-commit suite cadence
is waived while current final HEAD must remain green. The measured rollout,
peer/sector diagnostics, repaired workflow dispatch, and idempotent no-write run
are all complete.

## 0. Authoritative continuation status (2026-07-31)

This section supersedes any later placeholder or pending-status wording in this
document. The older sections remain as an audit trail of how M5 was developed.

### Final branch commits and artifacts

- Stock-Grader implementation/fix commit: `a136252`; workflow panel commit and
  current implementation-era head before this documentation update: `e9b4e92`.
- Stock-Vault implementation: `4a607e1`; collector artifact commit: `200c6e4`;
  exact private-spec byte pin and regression: `09e8b32`.
- Stock-Data ecosystem documentation: `01eddae`.
- No prior line in `research_ledger.jsonl` was edited. Exactly one record has
  experiment `universe:liq1000_v1`, verdict `PRE-REGISTERED`, and the full
  ledger chain verifies `True`.

### Green workflow and panel evidence

The repaired workflow was dispatched from `a136252` on
`codex/m5-wide-universe`. Run
[`30632292769`](https://github.com/TylerJForstrom/Stock-Grader/actions/runs/30632292769)
completed successfully and pushed panel commit `e9b4e92`. The exact required
`gh run list` line was:

```text
completed	success	monthly-freeze	monthly-freeze	codex/m5-wide-universe	workflow_dispatch	30632292769	17m47s	2026-07-31T12:53:20Z
```

The branch contains nine wide files for `2026-07-31`: `all_weather`,
`deep_value`, `dividend_growth`, `dividend_income`, `garp`, `growth`,
`quality`, `turnaround`, and `value`. Every file has exactly 1,000 rows,
`universe_id=liq1000_v1`, and
`universe_spec_sha256=bd02dccb979fd57cfc059e608be2bfea7bb7e7f3dd25ce34c1364c71371ebe94`.
Their graded counts range from 891 to 969. Every panel records
`code_commit=a136252`.

`low_volatility` and `momentum` are absent by design: the green run logged zero
gradable names out of 1,000 for each under SEC-only sparse prices. No safety
gate was weakened and no empty or misleading parquet was written.

A second dispatch,
[`30633530115`](https://github.com/TylerJForstrom/Stock-Grader/actions/runs/30633530115),
also completed successfully. Its exact `gh run list` line was:

```text
completed	success	monthly-freeze	monthly-freeze	codex/m5-wide-universe	workflow_dispatch	30633530115	13m9s	2026-07-31T13:12:28Z
```

That run logged all nine existing wide panels as `already frozen; nothing to
do`, retried only the two structural refusals, and the commit step printed
`nothing to commit`. This is the required second-invocation no-write/exit-zero
evidence.

### Final validation evidence

- Stock-Grader focused M5 set: `48 passed in 19.97s`.
- Stock-Grader pre-commit full run: `633 passed in 1090.09s (0:18:10)`.
- Stock-Grader post-`a136252` full run: `633 passed in 902.87s (0:15:02)`.
- Stock-Grader Ruff: `All checks passed!`.
- Stock-Vault `python -m pytest -q` exited 0 with:

  ```text
  ........................................................................ [ 75%]
  .......................                                                  [100%]
  ```

  This is 95 tests; its configured double-quiet output suppresses the count.
  Stock-Vault Ruff: `All checks passed!`.
- Stock-Data `python -m pytest -q` exited 0 with:

  ```text
  ......................................                                   [100%]
  ```

  This is 38 tests. Stock-Data Ruff: `All checks passed!`.

The owner explicitly waived the historical before/after-every-intermediate-commit
requirement on 2026-07-31. The audit fact remains: home commit `6bc96e3`
contained the Windows timing-newline defect and its new regression correctly
failed there. The defect is fixed in `a136252`; current final HEAD is required
to pass the full suite and Ruff. Do not rewrite history as if `6bc96e3` was
green; the waiver resolves the acceptance item without erasing the evidence.

### Provenance and regression closure

- Public spec bytes hash to `bd02dcc...ebe94`; the 1,000-name file has 1,000
  unique, sorted ticker-only members and no member contains a digit.
- Vault spec bytes are exactly 1,054 bytes and hash to
  `8bbeb245...ff48c`, matching the immutable private manifest. Commit
  `09e8b32` pins those bytes with `-text`; no private ranked artifact or
  manifest was rewritten.
- The peer-measurement JSON generator now records a stable basename, adds the
  input-table SHA-256, and writes canonical LF bytes. The four hashes in the
  historical table are still valid identifiers for the archived `6bc96e3`
  files, but are not cross-machine reproduction hashes. Corrected hashes
  require the off-git metadata export with SHA-256
  `b92ce934a22a9855ad73a77b3c637357a36e959af68efe893f989b90bb57cade`.
- ~~The workflow commit step no longer uses `if: always()`, so failed freezes
  cannot publish partial panels.~~ **Reverted 2026-07-31 — this reasoning was
  wrong and the change was a data-loss bug. See §0d.** Its retry
  fetches/rebases/pushes only the exact triggering branch, which was correct and
  is retained.
- Old-code proofs were captured: the timing regression failed on Windows CRLF;
  the peer artifact regression failed on absolute-path/native-newline output;
  and the workflow regression failed while `if: always()` was restored.

The peer diagnostic treats an explicitly blank derived `market_cap` as typed
missingness to mirror production peer selection; it never invents a numeric
default, never uses a missing cap in cap ratios, and reports missing-cap peers
explicitly. A present malformed/non-finite/non-positive value raises. Broker
and feed numeric parsers remain fail-closed.

### Deferred follow-up (not an M5 blocker)

The owner accepted the nine honest SEC-only panels as M5's public production
output. `momentum` and `low_volatility` remain registered lifetime research arms
and stay charged in the ledger's `trials=11`, but their public panels are deferred
to a separate milestone requiring a licensed point-in-time dense daily-price
artifact. Defining-pillar and letter-floor gates remain unchanged; no placeholder
panel may be manufactured.

### Ready-to-paste home-computer prompt

```text
Verify the completed M5 wide-universe milestone on my home Windows computer.

Repositories:
  C:/Users/tforstrom/Desktop/Stock-Grader   PUBLIC
  C:/Users/tforstrom/Desktop/Stock-Vault    PRIVATE; never make public
  C:/Users/tforstrom/Desktop/Stock-Data     PUBLIC

First pull all three repos and run git status. Stop if any tree has unexpected
changes. Work only on codex/m5-wide-universe; never push directly to main.

Read in order:
  Stock-Grader/docs/MAJOR_IMPROVEMENTS.md
  Stock-Grader/docs/majors/ORIENTATION.md
  Stock-Grader/docs/majors/M5-wide-universe.md
  Stock-Grader/docs/HANDOFF.md
  Stock-Grader/docs/M5_HANDOFF_HOME.md

Authoritative state: Grader implementation a136252 and panel commit e9b4e92;
Vault 09e8b32; Data 01eddae. Workflow runs 30632292769 and 30633530115 are
green. The second run skipped all nine existing wide panels and printed
`nothing to commit`. Verify these facts and artifact hashes; do not rebuild
completed M5 infrastructure.

The owner approved the M5 amendment on 2026-07-31: nine valid public SEC-only
panels are the complete production set. Momentum and low_volatility are deferred
to a separately licensed dense-price milestone and remain charged in lifetime
trial accounting. Do not reopen M5 to weaken gates, fabricate prices, edit old
ledger lines, overwrite dated artifacts, expose Vault-derived data, or mix
universe fingerprints.

If corrected peer JSON hashes are requested, first locate the exact off-git
metadata export whose SHA-256 is
b92ce934a22a9855ad73a77b3c637357a36e959af68efe893f989b90bb57cade;
otherwise preserve the four listed hashes as historical 6bc96e3 evidence.

Confirm the final branch is clean and synchronized, the current Grader full suite
and Ruff are green, and the two recorded workflow runs remain successful. Treat
M5 as complete under the owner-approved amendment. Any future dense-price work is
a separate milestone and must preserve the public/private licensing boundary.
```

## 0d. Audit corrections (2026-07-31, branch `fix/m5-audit-defects`)

An independent audit of `codex/m5-wide-universe` confirmed six high-severity
defects. All six are fixed on `fix/m5-audit-defects`, each with a regression
test proven to fail on the pre-fix code. **This section overrides anything
earlier in this document that describes the pre-fix behaviour as intended.**

| # | Defect | Fix |
|---|---|---|
| 1 | The universe rule was not point-in-time: candidates were screened through TODAY's exchange listing directory and misses were silently dropped, so anything delisted after the as-of date vanished. | `FoundryDataSource.symbol_directory` takes `asof` and replays the `nasdaqlisted`/`otherlisted` event stream backward, as `universe()` already did. A candidate with no directory row is still excluded — Stock-Data drops test issues at write time, so "missing" cannot be read as "eligible" — but it is now recorded, not silent. |
| 2 | Nothing compared the SEC bulk archive's generation to the as-of date. | `assert_archive_covers_asof` refuses an archive generated before EDGAR stopped accepting filings dated `asof` (floor `asof+1` 03:00 UTC, conservative against the 22:00 ET close) or more than 7 days after it. |
| 3 | The rank key accepted absurd and ancient values. | Spec-driven `max_public_float_usd` (default $10T) and `max_observation_age_days` (default 730), applied to the observation that would actually be selected, with every rejection logged and manifested. |
| 4 | Primary listings vanished with no warning or drop manifest. | Builds emit `universe_drops_<asof>.json` accounting for every exclusion; a shell CIK with no `dei` taxonomy falls back to the issuer's real filing CIK; an additive `issuer_ticker_rule` seats common equity ahead of notes and preferred series. |
| 5 | `monthly-freeze.yml` lost `if: always()` on Commit. | Restored on Commit and on the wide freeze. |
| 6 | `SECBulkFacts` compared only byte length on cache reuse. | Every reuse path re-hashes and compares against the recorded sha256, memoised per process. |

### Why the `if: always()` removal (§"Provenance and regression closure") was wrong

The stated justification — "so failed freezes cannot publish partial panels" —
does not hold against the code. `cmd_freeze` refuses a profile **before** writing
its parquet and writes each panel atomically (`tmp` + `replace`), so a partial
panel does not exist for the commit step to publish. What the removal did cause
is real: both freeze steps write their valid panels to disk before the alarm
policy runs, and all nine wide profiles now have a prior panel, so any refusal is
a regression and exits 2 — discarding every good panel from that run. Frozen
panels are point-in-time evidence that cannot be backfilled. The run still
reports red; that is the alarm.

### The committed 2026-07-30 artifacts are not rebuildable, and were not rewritten

The four hash-locked artifacts still hash to their registered values and were not
regenerated. But they cannot be reproduced, and this is now provable rather than
suspected: their `source_sha256` `21eae853…` derives from bulk archive SHA
`5d69b5a1…`, which is the **2026-07-28** generation, while the files declare
`asof 2026-07-30`. Rebuilding from the 2026-07-30 archive yields a different
N=1000 set (ACA out at rank 1001, APLD in at 792) and `source_sha256`
`c078ac25…`. Under the new §2 guard the 2026-07-30 archive is itself inadmissible
for `asof 2026-07-30` — it was generated 16:18 UTC, about noon Eastern, hours
before that day stopped producing filings — so a faithful rebuild of that date
requires an archive that no longer exists. Treat the committed files as immutable
historical inputs whose provenance is now documented, not as reproducible ones.

### Review corrections (commit `b10d7f7`)

An adversarial multi-agent review of the fix commit found four real defects in
the fix itself. All are corrected, and all were verified against the real
archive before and after:

- The recency bound keyed on the XBRL `end` tag rather than the filing date.
  Real filers repeat a stale `end` — AMD's 10-K filed 2026-02-04 still carries
  `end=2024-06-28` — so 22 actively-filing issuers were dropped and labelled
  dormant. It now gates on `filed`. AMD returns at rank 49; genuinely dormant
  tags still go.
- `duplicate_cik` asserted retentions that never happened (429 false records).
  A CIK is now claimed only when a candidate is actually ranked, so a security
  that fails the float check no longer takes its issuer's seat down with it.
- The drop manifest listed seated tickers. `notes` is now separate from `drops`,
  so drops are exclusions and are disjoint from the universe.
- The workflow regression test could be satisfied by its own comment quoting
  `if: always()`. It now matches the step key on a non-comment line.

### Known residual (not fixed, and not silently ignored)

A scalar plausibility ceiling cannot separate every scale-error filing from a
real megacap. The ceiling removes Cabot Corp ($4.43 QUADRILLION, previously
**rank 1**) and Fresenius ($16.5T on a 2016 observation), and the recency bound
removes 13 stale rankings. Nine filings remain in the $1.9T–$6.8T band — OLED,
ONTO, TTMI, SKY, NOVT, MGRC, ENVA, RENX, CLSK — each roughly 1000x its true
float, and that band overlaps genuine megacaps (NVDA $4.00T, MSFT $3.60T, AAPL
$3.25T). Separating them needs a second signal, such as an intra-issuer
consistency check against the filer's own float history or a cross-check against
`us-gaap` revenue. That is a deliberate follow-up, not an oversight.

BRK-A still seats ahead of BRK-B: both are described as common stock by Nasdaq
Trader, so no public-domain field distinguishes them. The displacement is now
recorded in the drop manifest rather than silent. A share-count or price
tiebreak would need the private vault archive, which cannot enter a public
universe under the licensing split.

## 0b. Running any command in this document on macOS or Linux

This document was written on Windows and every command block below is
PowerShell. The commands are correct, but three things must be translated or
they fail — or worse, succeed in the wrong place.

**Scratch paths.** `C:/tmp/whatever` is an *absolute* path on Windows and a
*relative* one on POSIX: `Path("C:/tmp/m5-stage-250").is_absolute()` is `False`
there, and the parts are `("C:", "tmp", "m5-stage-250")`. A stage run would
therefore create a directory literally named `C:` **inside the public repo** and
write scratch panels into it. Substitute a real scratch root everywhere a
`C:/tmp/...` path appears:

```bash
SCRATCH="${TMPDIR:-/tmp}/m5"; mkdir -p "$SCRATCH"
# ...then use "$SCRATCH/m5-stage-250" wherever the block says C:/tmp/m5-stage-250
```

**Line continuations.** PowerShell continues a line with a trailing backtick;
`sh`/`zsh` use a trailing backslash. Translate every `` ` `` at end of line to `\`.

**Interpreter.** Use the repo's virtualenv explicitly (`.venv/bin/python -m ...`)
rather than a bare `python`: Homebrew's Python refuses `pip install -e .` outside
a venv (PEP 668), and `/usr/bin/python3` is 3.9, below this project's floor.

Everything else is portable: `data/cache.py` dispatches on `os.name`, no source
file in any of the three repos hardcodes a `C:` path, and a forced-LF checkout
reproduces every hash-locked artifact byte-for-byte (verified: the four universe
artifacts, all 27 parquet panels, and every Stock-Data manifest). The one
platform-specific harness, `scripts/measure_wide_freeze.py`, now dispatches its
peak-RSS reading per platform instead of assuming Win32.

## 0c. First-run setup on macOS

Verified against a forced-LF checkout of all three repos (`core.autocrlf=false`,
`core.eol=lf`): every hash-locked artifact reproduced byte-for-byte and all
three suites passed (Grader 633, Vault 95, Data 38), so a Mac checkout starts
from an identical state — `git status` comes up clean, with no line-ending noise.

```bash
# 1. Toolchain
xcode-select --install
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12 ripgrep gh    # ripgrep is used by the handoff procedures

# 2. GitHub access (all three remotes are SSH)
ssh-keygen -t ed25519 -C 'tylerjamesforstrom@gmail.com'
gh auth login --hostname github.com --git-protocol ssh --web
gh ssh-key add ~/.ssh/id_ed25519.pub    # or paste it at github.com/settings/keys

# 3. Clone
mkdir -p ~/dev && cd ~/dev
git clone git@github.com:TylerJForstrom/Stock-Grader.git
git clone git@github.com:TylerJForstrom/Stock-Data.git
git clone git@github.com:TylerJForstrom/Stock-Vault.git   # private; not needed for M5 work

# 4. Per repo: a venv is REQUIRED, not a nicety
#    Homebrew python refuses `pip install -e .` outside a venv (PEP 668), and
#    /usr/bin/python3 is 3.9, below this project's requires-python floor.
for r in Stock-Grader Stock-Data Stock-Vault; do
  cd ~/dev/$r && python3.12 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
done

# 5. Environment (add to ~/.zshrc)
export STOCK_GRADER_CONTACT='tylerjamesforstrom@gmail.com'   # SEC User-Agent; see the warning below
# Vault collectors only — the Grader needs none of these:
# export MASSIVE_API_KEY=... FINNHUB_API_KEY=... ALPHAVANTAGE_API_KEY=...
# ALPACA_PAPER_* stay GitHub-only: the paper trader runs in Actions, not locally.

# 6. Verify before changing anything
cd ~/dev/Stock-Grader
shasum -a 256 config/universe_spec.json config/universe_liq*_2026-07-30.txt
# expect bd02dccb…, b2005b1e…, da00b7d1…, 9d174b5b…
.venv/bin/python -m pytest -q      # expect 633 passed (~20-25 min)
.venv/bin/python -m ruff check .   # expect All checks passed!
```

**Set `STOCK_GRADER_CONTACT` before any networked run.** The workflow hard-fails
without it, but the library does not: `SECClient` falls back to a placeholder
User-Agent and logs nothing. A thousand rate-limited SEC requests plus a 1.33 GB
bulk download under a placeholder identity is what SEC's automated-access policy
exists to block, and the failure surfaces as an opaque 403 rather than as "you
forgot the contact".

**The cache is ~1.9 GB and does not travel.** It lives at
`%LOCALAPPDATA%\stock-grader` on Windows and `~/.cache/stock-grader` on macOS
(not `~/Library/Caches` — `data/cache.py` uses the XDG convention). Contents
measured on the Windows box: 1,092 files / 1,872.7 MB, of which
`bulk/companyfacts_2026-07-30.zip` is 1,327.2 MB. Either let the Mac re-download
(20-40 min on home broadband; the archive is re-fetched on each new UTC day
anyway) or copy the directory across first — the sidecar validates by size and
digest, so a copied archive is accepted.

## 1. Safety first on the home computer

The three repositories are:

| Repository | Local path (Windows) | Visibility |
|---|---|---|
| Stock-Grader | `C:/Users/tforstrom/Desktop/Stock-Grader` | PUBLIC |
| Stock-Vault | `C:/Users/tforstrom/Desktop/Stock-Vault` | PRIVATE; never make public |
| Stock-Data | `C:/Users/tforstrom/Desktop/Stock-Data` | PUBLIC |

On a Mac these live wherever they were cloned (`~/dev/<repo>` in the setup
below). Paths in this document are Windows-absolute because that is where the
work was done; translate them to your checkout root.

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
- Initial Agent-log/handoff update: `806e896`.
- Measurement and validation documentation: `b11683f`, `392e4e2`, and
  `6bc96e3`.
- Workflow/artifact reproducibility repair: `a136252`.
- Verified workflow panel commit: `e9b4e92`.
- Remote after push: `origin/codex/m5-wide-universe` at `e9b4e92` before this
  documentation-only update.

### Stock-Vault

- Branch: `codex/m5-wide-universe`
- M5 collector implementation commit: `4a607e1`
- Current branch HEAD after exact spec-byte pinning: `09e8b32`
- Remote: `origin/codex/m5-wide-universe`
- Working tree was clean during the final independent audit.

The `200c6e4` commit is the normal data/heartbeat commit produced by the
successful collector workflow; the M5 code itself is in `4a607e1`, and
`09e8b32` pins the already-registered spec bytes without editing `data/`.

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
additive issuer-ticker and observation rules. The registered hash is the exact
CRLF byte representation created on Windows. Git had previously normalized the
committed blob to LF, whose different SHA-256 is
`d1c876cbbd4e8006185f9efd098e2342e6ee286b635a53ad26e5b0d352d377d2`.
The continuation fix commits the registered CRLF bytes and pins the spec and
membership paths as `-text` in `.gitattributes`; the membership hashes below
remain unchanged. `tests/test_m5_artifact_provenance.py` now locks all four raw
hashes on every platform.

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

The private and public spec hashes deliberately differ because they describe
different licensed rules. The audit found Git had normalized the private spec
blob even though the Windows checkout happened to match the immutable
manifest. Vault commit `09e8b32` stages the exact registered 1,054 bytes, whose
SHA-256 is `8bbeb245...ff48c`, and adds a `-text` attribute plus regression.
The immutable ranked file and manifest were not edited; current checkout bytes
now match the manifest on every platform.

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

Stock-Grader run `30579474350` was dispatched from
`codex/m5-wide-universe` at `806e896`. Both freeze steps succeeded and the wide
step generated nine panels while honestly refusing `momentum` and
`low_volatility`, but the run concluded `failure` in job `90995842573`. The
runner committed the panels locally as `7ebeb32`; its push lost a concurrent
branch race, and the old retry incorrectly ran
`git pull --rebase origin main` in a shallow checkout. The resulting rebase
conflicts prevented any panel from reaching the branch.

The repaired workflow checks out full history, derives the exact triggering
branch from `GITHUB_REF_NAME`, fetches and rebases that remote branch before
every explicit push attempt, never substitutes `main`, and omits `if: always()`
from the commit step so a failed freeze cannot publish partial output. Run
`30579474350` remains useful failure evidence; runs `30632292769` and
`30633530115` are the acceptance evidence.

> **FINAL Stock-Grader workflow evidence:**
>
> - Repair commit: `a136252`
> - First green run: `30632292769`
> - URL: `https://github.com/TylerJForstrom/Stock-Grader/actions/runs/30632292769`
> - Conclusion: `success`; exact `gh run list` duration: `17m47s`
> - Bot panel commit: `e9b4e92`
> - Wide files: nine (`all_weather`, `deep_value`, `dividend_growth`,
>   `dividend_income`, `garp`, `growth`, `quality`, `turnaround`, `value`)
> - Every wide file: 1,000 rows, `liq1000_v1`, registered spec SHA
> - Second green run: `30633530115`; exact list duration: `13m9s`
> - Second result: nine panels skipped as already frozen; only the two known
>   structural refusals were recomputed; commit printed `nothing to commit`.

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

Post-artifact high-risk targeted set:

```text
186 passed in 624.37s (0:10:24)
All checks passed!
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

Pre-`6bc96e3` post-artifact validation:

```text
622 passed in 681.92s (0:11:21)
All checks passed!
```

Current repair-tree pre-commit validation:

```text
633 passed in 1090.09s (0:18:10)
```

Post-`a136252` validation:

```text
633 passed in 902.87s (0:15:02)
All checks passed!
```

### Stock-Vault

`python -m pytest -q` exited 0:

```text
........................................................................ [ 75%]
.......................                                                  [100%]
```

This is 95 tests. The repository's pytest configuration suppresses a separate
`95 passed` summary.

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
| Reusable wide-freeze harness | At `c276a0a`, collection failed with `ImportError: cannot import name 'measure_wide_freeze' from 'scripts'`; pytest exit 2. |
| Trigger-branch workflow retry | At `806e896`, the regression first failed because `fetch-depth: 0` was absent and exposed the old `git pull --rebase origin main`; pytest exit 1. |
| Failed-freeze commit guard | With the new assertion and the `6bc96e3` workflow restored via `git show`, the test failed because `if: always()` remained in the Commit step: `1 failed in 25.13s`. |
| Wide timing LF bytes | On the `6bc96e3` Windows `write_text` implementation, the deterministic timing test observed CRLF bytes instead of LF and failed in the 15-test focused run. |
| Portable peer artifacts | Against the unchanged `6bc96e3` script, the two-path CLI regression produced different absolute-path/native-newline JSON: `1 failed in 3.68s`. |
| Vault registered spec bytes | Before `09e8b32`, the focused test failed because `.gitattributes` did not pin the spec; the staged old blob hashed to `741168...` rather than manifest hash `8bbeb...`. |
| Cross-platform artifact bytes | At `392e4e2`, the raw spec SHA was `d1c876...`, not the registered `bd02dcc...`; pytest exit 1. |
| Missing-cap and sector diagnostics | At `392e4e2`, the exact new test file failed collection because `measure_sector_key_concentration` did not exist. Isolating the behavioral tests then failed on the absent peer-count schema and the old loader's rejection of a missing cap; pytest exit 1. |

If any new behavior is added while finishing the measured rollout, repeat the
same old-file failure proof before committing it. Do not treat "the old code
obviously lacked the function" as a substitute for the requested empirical
failure.

## 9. Research ledger

Exactly one record is committed on the M5 branch in `27b385d`:

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

6. **The original exact-eleven SEC-only requirement conflicts with the
   defining-pillar gates.** `cmd_freeze` safely refuses the two price-defined
   profiles and treats the unchanged state as idempotent. The owner's explicit
   2026-07-31 amendment resolves this M5 acceptance conflict by accepting the
   nine valid public panels and deferring `momentum` and `low_volatility` to a
   licensed dense-price milestone. The data limitation remains documented;
   the gates remain intact.

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

15. **The registered public spec hash was a Windows working-tree hash, not the
    normalized Git blob hash.** The raw CRLF bytes hash to the registered
    `bd02dcc...`; Git's previous LF blob hashed to `d1c876...`. The final
    `.gitattributes` rule and raw-hash regression preserve the registered bytes
    across platforms without changing the logical JSON or membership files.

16. **The private spec blob mismatch is resolved without rewriting data.**
    Vault `09e8b32` commits the exact 1,054 registered bytes, adds `-text`, and
    proves SHA `8bbeb...` equals the immutable manifest. The ranked file and
    manifest remain byte-identical to their historical versions.

17. **The continuation runtime exposed a macOS workspace despite the Windows
    handoff target.** The expensive N=250/500/1000 timings remain the recorded
    Windows measurements. The continuation used the exact cloned commits and
    Python 3.12, and the final cloud proof runs on GitHub's Linux runner.

18. **The four peer JSON hashes are historical, not portable.** `6bc96e3`
    embedded an absolute source path and native newlines. `a136252` writes LF,
    records a stable basename, and adds the source SHA. Corrected hashes need
    the off-git input with SHA `b92ce934...cade`; the documented numerical
    decisions remain valid.

## 11. Current acceptance status

| Acceptance item | Status at handoff |
|---|---|
| Current final-HEAD Grader suite and historical cadence amendment | **Met under owner-approved amendment:** current final HEAD is green; the waived `6bc96e3` Windows timing regression remains disclosed |
| Final Grader suite and Ruff | Met: 633 passed in 902.87s; Ruff clean |
| Vault suite and fixture screen | Met |
| Public spec | Met |
| Public 1,000-name file | Met: generated, hash-verified, committed in `27b385d` |
| Multi-profile equality test | Met |
| 82-name speedup <=25% | Met: 17.4% |
| Clean dated-frame cache equality | Met |
| SEC bulk regression set | Met; final CDN addition has 14 focused passes |
| Vault panel one-read/exact-series test | Met |
| Mixed-universe guard | Met |
| Owner-amended nine-panel SEC-only production set + second exit 0 | **Met:** nine valid 1,000-row panels are committed; the two price-defined profiles safely refuse with no placeholders; run `30633530115` proves exit 0/no write |
| N=250/500/1000 timing table | Met; measured rows are below and in `docs/UNIVERSE.md` |
| N=1000 under 100 minutes | Met: 1,253.896 seconds (20.90 minutes) |
| Grader workflow branch dispatch green | Met: `30632292769` and idempotence run `30633530115` both succeeded |
| Exactly one pre-registration ledger record + valid chain | Met and committed in `27b385d` |
| Coverage/graded and retuning measurements documented | Met: coverage retains 0.35; fixed peer grid retains 8/30/5x; measured sector concentration retains `business_model`; 15-name outage floor retained |
| Ecosystem and Vault docs | Met |
| `docs/REVIEW_FEEDBACK.md` M5 commit-hash line | Met; refreshed through Grader `e9b4e92`, Vault `09e8b32`, Data `01eddae` |

M5 is complete under the owner's explicit 2026-07-31 acceptance amendment and final-HEAD validation requirement.

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

## 13. Historical completion procedure (completed)

The steps below are retained as reproducibility and audit instructions. Their
implementation, measured runs, workflow verification, and owner-approved
acceptance amendment are complete; they are not an active M5 work queue.

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

This step is now complete. The exact metadata export SHA-256 is
`b92ce934a22a9855ad73a77b3c637357a36e959af68efe893f989b90bb57cade`;
it contains 1,000 fundamentals-bearing snapshots, 957 usable target market
caps, and 994 usable SICs. The fixed seed-0 grid produced no insufficient
target under any of 8/5x, 12/5x, 8/3x, or 12/3x. Only 8/5x kept every selected
known-cap peer inside the requested band; the other settings forced 45, 40,
and 100 outside-band peers, respectively. The production peer settings are
therefore left unchanged at 8/30/5x.

GENERAL contains 703/1,000 names. `business_model`, SIC2, and SIC3 form 7, 62,
and 173 groups with HHI 0.509956, 0.050184, and 0.024693. SIC2 has 8 singleton
groups and 266 names in groups below 15; SIC3 has 46 singletons and 500 names
in groups below 15. The production sector key remains `business_model`; the
15-name outage floor and coverage gate 0.35 also remain unchanged. Exact grid
tables and JSON hashes are in `docs/UNIVERSE.md`.

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

Record the known SEC-only profile limitation without weakening gates. The
owner-approved criterion change accepts nine public panels and the verified
second-run no-write/exit-zero behavior; dense-price work is a later milestone.

### Step 12: final repository audit

```powershell
git -C C:/Users/tforstrom/Desktop/Stock-Grader status --short
git -C C:/Users/tforstrom/Desktop/Stock-Vault status --short
git -C C:/Users/tforstrom/Desktop/Stock-Data status --short

git -C C:/Users/tforstrom/Desktop/Stock-Grader log --oneline --decorate -8
git -C C:/Users/tforstrom/Desktop/Stock-Vault log --oneline --decorate -8
git -C C:/Users/tforstrom/Desktop/Stock-Data log --oneline --decorate -8
```

Verify the ledger chain one final time and report every amended acceptance item
with evidence. Under the owner's 2026-07-31 amendment, the nine-panel public
output and disclosed two-profile deferral complete M5 once final HEAD is green.

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

## 15. Historical continuation prompt (superseded)

Use the authoritative ready-to-paste prompt in section 0. The block below is
retained only to show the pre-verification handoff state.

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
  2. verify the completed peer/sector grid, its four JSON hashes, and the
     documented no-change decisions;
  3. run any still-pending final Grader full suite and Ruff and preserve exact
     summaries;
  4. if the repaired workflow has not yet been proved green, dispatch
     monthly-freeze.yml on the branch and verify
     the completed run with gh run list;
  5. perform the final three-repo status and acceptance audit.

Use real data only. Do not weaken safety gates, edit prior ledger lines,
overwrite immutable artifacts, pool universe IDs, leak Vault-derived data into
public repos, or shard scoring by ticker.

The milestone contains a known unresolved contradiction: the specified
SEC-only command cannot honestly write exactly eleven panels. Its unchanged
structural-refusal rerun is now idempotent and exits 0, but SEC sparse prices
still cannot create momentum or low-volatility evidence. Do not claim M5
complete unless a dense licensed source or an explicit acceptance amendment
resolves the exact-eleven criterion. Report the contradiction precisely.

When done, report every M5 acceptance criterion as met/not met with evidence,
the exact pytest/Ruff summary lines for all touched repos, all discrepancies,
and every remaining blocker. Do not report M5 complete unless every criterion
is genuinely met.
```
