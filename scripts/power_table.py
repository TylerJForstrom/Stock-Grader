#!/usr/bin/env python3
"""Planted-IC power table: calibrate the monthly forward-backtest gate.

The monthly forward backtest (``.github/workflows/monthly-forward-backtest.yml``)
evaluates each profile's matured panel with ``stock-grader backtest`` and records
a gate verdict (``significant`` = DSR >= 0.95 AND bootstrap Sharpe CI low > 0).
Before reading the first real verdicts (~September 2026, ``--min-periods 3``)
we must know what that gate can and cannot see.

This script answers that with a grid of fully synthetic panels whose true
cross-sectional rank IC is KNOWN because it was planted (exported by the
Stock-Market-Sim calibration generator; see the grid's ``manifest.json``).
Every replication is pushed through the SAME code path production uses — the
``backtest`` CLI subcommand with the workflow's exact flags — never a private
re-implementation of the statistics.

Design decisions, stated so the table cannot be over-read:

* One FRESH scratch ledger per replication. With a single trial the E[max]
  benchmark is 0, so DSR == PSR: this measures the gate at its most
  permissive. The real ledger accumulates trials every month, so the real
  deflation is at least as harsh and real power is <= the tabulated power.
* "False positive" and "power" are rates of ``significance.significant``
  (exactly what the workflow records as ``gate_passed``) across replications.
* The real ledger is never touched. The script refuses to write any file
  inside the repository checkout except the dated artifact itself.

Usage:

    python scripts/power_table.py \
        --grid-dir /path/to/calibration_panels/2026-08-03 \
        --scratch-dir /path/outside/the/repo \
        --out-dir docs/calibration

Dated artifacts are immutable: the script refuses to overwrite existing
outputs.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from stock_grader import cli as grader_cli
from stock_grader.research_manifest import current_commit

REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact evaluation flags monthly-forward-backtest.yml passes to
# `stock-grader backtest`. --format json instead of md only changes the
# serialization of the identical computation (the workflow's md report and
# this JSON payload are rendered from the same BacktestReport and
# SignificanceReport objects).
PRODUCTION_BACKTEST_FLAGS: tuple[str, ...] = (
    "--periods-per-year",
    "12",
    "--min-cross-section",
    "20",
    "--quantiles",
    "5",
    "--allow-unverified-panel",
)

GATE_ALPHA = 0.05  # assess_edge default: significant = DSR >= 0.95 and CI low > 0

#: Detection probability a cell must reach to count as "detectable".
DETECTION_THRESHOLD = 0.5


def backtest_flags(periods_per_year: int) -> tuple[str, ...]:
    """Production flags with the annualisation set to a grid's own cadence.

    Only ``--periods-per-year`` varies, and it CANNOT change a gate verdict:
    ``significant`` is ``DSR >= 0.95 and sharpe_ci_low > 0``; the PSR/DSR side
    runs on ``per_period_sharpe`` (mean/sd, unannualised), and the bootstrap
    side multiplies every resampled Sharpe by ``sqrt(periods_per_year)``, a
    positive constant that cannot move the sign of the interval's lower
    bound. It is set correctly anyway so the reported annualised Sharpes mean
    what they say.
    """

    flags = list(PRODUCTION_BACKTEST_FLAGS)
    flags[flags.index("--periods-per-year") + 1] = str(periods_per_year)
    return tuple(flags)


@dataclass(frozen=True, slots=True)
class SeedOutcome:
    """One replication's trip through the production gate."""

    seed: int
    gate_passed: bool
    dsr: float | None
    psr_vs_zero: float | None
    sharpe_ci_low: float | None
    mean_rank_ic: float
    verdict: str


@dataclass(slots=True)
class CellResult:
    """Aggregate gate behaviour for one (planted IC, periods, universe) cell.

    ``months`` is the v1 field name for the per-replication PERIOD count and
    is kept so the 2026-08-03 artifact's schema still parses; on a
    semi-monthly grid the periods are not months. ``label``, ``regime`` and
    ``cadence`` are empty for grids that predate those axes.
    """

    planted_ic: float
    months: int
    universe: int
    file: str
    input_sha256: str
    n_seeds: int
    gate_pass_rate: float
    dsr_pass_rate: float
    ci_low_positive_rate: float
    mean_dsr: float | None
    mean_realized_rank_ic: float
    insufficient_sample_verdicts: int
    label: str = ""
    regime: str = ""
    cadence: str = ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_outside_repo(path: Path, *, what: str) -> None:
    """Refuse scratch locations inside the checkout: the real ledger lives here."""

    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ValueError(
            f"{what} ({resolved}) is inside the repository checkout; exploratory "
            "ledgers and per-seed panels must live in a scratch directory so the "
            "append-only research_ledger.jsonl can never be touched by mistake"
        )


def evaluate_seed_panel(
    panel: pd.DataFrame,
    workdir: Path,
    label: str,
    *,
    flags: tuple[str, ...] = PRODUCTION_BACKTEST_FLAGS,
) -> dict:
    """Run one replication through the production CLI path; return its JSON payload."""

    assert_outside_repo(workdir, what="scratch workdir")
    workdir.mkdir(parents=True, exist_ok=True)
    panel_path = workdir / f"{label}.parquet"
    ledger_path = workdir / f"{label}.ledger.jsonl"
    # Fresh ledger per replication (see module docstring: DSR == PSR here).
    ledger_path.unlink(missing_ok=True)
    panel.to_parquet(panel_path, index=False)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        returncode = grader_cli.main(
            [
                "backtest",
                str(panel_path),
                "--ledger",
                str(ledger_path),
                *flags,
                # NOT part of PRODUCTION_BACKTEST_FLAGS: that tuple is quoted
                # verbatim into a dated calibration artifact, which is
                # immutable. This is a property of the SCRATCH panel, not of
                # the evaluation — these synthetic grids are written to a temp
                # dir that no producer catalogs, so the strict manifest check
                # would refuse them.
                "--allow-unmanifested-panel",
                "--format",
                "json",
            ]
        )
    if returncode != 0:
        raise RuntimeError(f"backtest CLI failed for {label} (exit {returncode})")
    payload = json.loads(stdout.getvalue())
    panel_path.unlink()  # keep the scratch footprint bounded
    return payload


def evaluate_cell_frame(
    frame: pd.DataFrame,
    workdir: Path,
    label: str,
    *,
    flags: tuple[str, ...] = PRODUCTION_BACKTEST_FLAGS,
) -> list[SeedOutcome]:
    """Split a stacked cell by ``seed`` and evaluate each replication."""

    outcomes: list[SeedOutcome] = []
    for seed_value, group in frame.groupby("seed", sort=True):
        panel = group.drop(columns=["seed"]).reset_index(drop=True)
        payload = evaluate_seed_panel(
            panel, workdir, f"{label}_s{int(seed_value):04d}", flags=flags
        )
        significance = payload.get("significance")
        if significance is None:
            raise RuntimeError(
                f"{label} seed {seed_value}: no significance block (fewer than 2 "
                "accepted periods); the grid cell does not match its manifest"
            )
        outcomes.append(
            SeedOutcome(
                seed=int(seed_value),
                gate_passed=bool(significance["significant"]),
                dsr=significance.get("deflated_sharpe"),
                psr_vs_zero=significance.get("psr_vs_zero"),
                sharpe_ci_low=significance.get("sharpe_ci_low"),
                mean_rank_ic=float(payload["mean_rank_ic"]),
                verdict=str(significance.get("verdict", "")),
            )
        )
    return outcomes


def _finite_mean(values: list[float | None]) -> float | None:
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return sum(finite) / len(finite) if finite else None


def summarize_cell(cell: dict, outcomes: list[SeedOutcome]) -> CellResult:
    n = len(outcomes)
    if n == 0:
        raise ValueError(f"cell {cell['file']} produced no replications")
    return CellResult(
        planted_ic=float(cell["planted_ic"]),
        months=int(cell["months"]),
        universe=int(cell["universe"]),
        file=str(cell["file"]),
        input_sha256=str(cell["sha256"]),
        n_seeds=n,
        gate_pass_rate=sum(o.gate_passed for o in outcomes) / n,
        dsr_pass_rate=sum(
            1
            for o in outcomes
            if o.dsr is not None and math.isfinite(o.dsr) and o.dsr >= 1.0 - GATE_ALPHA
        )
        / n,
        ci_low_positive_rate=sum(
            1
            for o in outcomes
            if o.sharpe_ci_low is not None
            and math.isfinite(o.sharpe_ci_low)
            and o.sharpe_ci_low > 0.0
        )
        / n,
        mean_dsr=_finite_mean([o.dsr for o in outcomes]),
        mean_realized_rank_ic=sum(o.mean_rank_ic for o in outcomes) / n,
        insufficient_sample_verdicts=sum("INSUFFICIENT SAMPLE" in o.verdict for o in outcomes),
        label=str(cell.get("label", "")),
        regime=str(cell.get("regime", "")),
        cadence=str(cell.get("cadence", "")),
    )


def run_grid(
    grid_dir: Path,
    scratch_dir: Path,
    *,
    verify_hashes: bool = True,
    progress: bool = True,
    periods_per_year: int = 12,
) -> tuple[dict, list[CellResult]]:
    """Evaluate every cell of a calibration grid; returns (manifest, results)."""

    assert_outside_repo(scratch_dir, what="scratch directory")
    flags = backtest_flags(periods_per_year)
    manifest_path = grid_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    results: list[CellResult] = []
    for cell in manifest["cells"]:
        cell_path = grid_dir / cell["file"]
        if verify_hashes:
            actual = sha256_file(cell_path)
            if actual != cell["sha256"]:
                raise ValueError(
                    f"{cell['file']}: sha256 mismatch (manifest {cell['sha256']}, "
                    f"file {actual}); refusing to build a table from unverified inputs"
                )
        frame = pd.read_parquet(cell_path)
        label = cell_path.stem
        outcomes = evaluate_cell_frame(frame, scratch_dir / label, label, flags=flags)
        result = summarize_cell(cell, outcomes)
        results.append(result)
        if progress:
            print(
                f"{label}: gate {result.gate_pass_rate:.2f} "
                f"(dsr {result.dsr_pass_rate:.2f}, ci {result.ci_low_positive_rate:.2f}) "
                f"over {result.n_seeds} seeds",
                file=sys.stderr,
            )
    return manifest, results


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _grid_table(
    results: list[CellResult],
    *,
    universe: int,
    months_axis: list[int],
    ics_axis: list[float],
) -> list[str]:
    lines = [
        "| planted rank IC | " + " | ".join(f"{m} mo" for m in months_axis) + " |",
        "|---|" + "---|" * len(months_axis),
    ]
    by_key = {(r.planted_ic, r.months, r.universe): r for r in results}
    for ic in ics_axis:
        row = [f"| {ic:.2f}"]
        for months in months_axis:
            cell = by_key.get((ic, months, universe))
            row.append(_rate(cell.gate_pass_rate if cell else None))
        lines.append(" | ".join(row) + " |")
    return lines


def smallest_detectable_ic(
    results: Sequence[CellResult],
    *,
    threshold: float = DETECTION_THRESHOLD,
) -> float | None:
    """Lowest planted IC in ``results`` the gate caught at >= ``threshold``.

    ``None`` means "nothing in the tested range", which is a real answer and
    must be rendered as such rather than as the top of the grid.
    """

    candidates = sorted(
        r.planted_ic for r in results if r.planted_ic > 0 and r.gate_pass_rate >= threshold
    )
    return candidates[0] if candidates else None


def _band_key(result: CellResult) -> tuple[str, str, int]:
    return (result.label, result.regime, result.months)


def _select(
    results: Sequence[CellResult], *, label: str, regime: str, periods: int
) -> list[CellResult]:
    return [r for r in results if _band_key(r) == (label, regime, periods)]


def _power_row(cells: Sequence[CellResult], ics: Sequence[float]) -> str:
    by_ic = {r.planted_ic: r for r in cells}
    return " | ".join(_rate(c.gate_pass_rate if (c := by_ic.get(ic)) else None) for ic in ics)


def _mdi_text(value: float | None) -> str:
    return "none in the tested range" if value is None else f"{value:.2f}"


def render_band_markdown(
    manifest: dict,
    results: list[CellResult],
    *,
    date_tag: str,
    periods_per_year: int,
    primary_regime: str,
    primary_periods: int,
) -> str:
    """Render the band-labelled report for a grid whose cells carry labels."""

    bands = sorted({r.label for r in results if r.label})
    # Primary first everywhere it is listed: a reader should meet the regime
    # the per-band reading is written against before any contrast.
    regimes = sorted(
        {r.regime for r in results if r.regime},
        key=lambda name: (name != primary_regime, name),
    )
    period_axis = sorted({r.months for r in results}, reverse=True)
    generator = manifest.get("generator", {})
    regime_specs = generator.get("regimes", {})
    grid_spec = manifest.get("grid", {})

    def universe_of(label: str) -> int | None:
        sizes = {r.universe for r in results if r.label == label}
        return next(iter(sizes)) if len(sizes) == 1 else None

    lines: list[str] = [
        f"# Banded detection-power table — {date_tag}",
        "",
        "Recalibration of the forward-backtest significance gate "
        "(`stock-grader backtest`: PSR/DSR + block-bootstrap Sharpe CI, "
        "`significant` = DSR >= 0.95 AND CI low > 0) for a cross-section split "
        "into liquidity bands, against fully synthetic panels with a KNOWN "
        "planted cross-sectional rank IC.",
        "",
        "This is a NEW dated artifact. It does not replace, amend or correct "
        "`power_table_2026-08-03.{md,json}`, which stands unchanged and still "
        "describes the monthly, single-regime, 250/1000-name grid it was built "
        "from. Neither table's numbers transfer to the other's shape.",
        "",
        "## Why the earlier table does not describe these bands",
        "",
        "The 2026-08-03 grid is monthly, homogeneous in volatility, and sized "
        "250 or 1000. The bands here differ on all three: a twice-monthly "
        "settlement cadence, per-band cross-sections that are neither of those "
        "sizes, and a return process with volatility that varies ACROSS names. "
        "Reading a band's power off the old table would mean assuming those "
        "three differences cancel, and nothing establishes that.",
        "",
        "## How cross-sectional dispersion enters, which is not the obvious way",
        "",
        "The instinct is that raising dispersion is what makes a small-cap "
        "grid a small-cap grid. Half of that is wrong, and the half that is "
        "right is not right for the reason it looks like.",
        "",
        "Start from what the gate reads. `significant` is `DSR >= 0.95 AND "
        "sharpe_ci_low > 0`, computed on the quintile spread portfolio's "
        "per-period NET returns. The cross-sectional rank IC is computed on "
        "ranks and is therefore **exactly** invariant to any positive "
        "rescaling of returns. `per_period_sharpe` is mean/sd, the PSR's skew "
        "and kurtosis corrections are scale-free, and the bootstrap resamples "
        "the same series — so a GROSS Sharpe is scale-invariant too.",
        "",
        "**But the returns are not gross.** `evaluate_walk_forward` computes "
        "`net_spread = gross_spread - cost_rate * turnover`, and that "
        "deduction is in return units: it does NOT scale when the returns do. "
        "Multiply every forward return by `k` and the net series becomes "
        "`k * gross - cost`, whose Sharpe is not the original one. Raising the "
        "volatility level alone therefore shrinks the cost as a fraction of "
        "the spread and **mechanically helps detection**. The level is not a "
        "no-op, and a table that assumed it was would be quietly wrong.",
        "",
        "That matters here in the specific direction that flatters a "
        "small-cap result. If a band is modelled as more volatile while the "
        "charge stays fixed, its measured power rises for a reason that has "
        "nothing to do with signal. In reality the smaller bands carry LARGER "
        "costs, not the same ones, so the level effect measured below points "
        "the opposite way from the truth and this table's small-band power is "
        "an overstatement on that count.",
        "",
        "So the regime axis is run as a decomposition rather than one contrast:",
        "",
        "1. **Volatility level** (`baseline-largecap` vs "
        "`smallcap-homogeneous`, identical in every other respect). Enters "
        "only through the cost-to-dispersion ratio, as above.",
        "2. **Volatility dispersion across names** (`smallcap-homogeneous` vs "
        "`smallcap-heterogeneous`, at an identical level). When names differ "
        "in volatility, an equal-weighted quintile portfolio's period return "
        "is dominated by its most volatile members, whose membership is driven "
        "by volatility rather than by the score. That weakens the map from a "
        "given rank IC to a portfolio Sharpe, which is what the gate reads. "
        "Scale-free, and it works against detection.",
        "3. **Tail index** (varied alongside dispersion in the same pair). "
        "Fatter per-name tails feed the PSR's skew/kurtosis correction "
        "directly and inflate the sd in the Sharpe denominator. Also "
        "scale-free.",
        "4. **Cross-section size**, which sets the per-period rank-IC standard "
        "error at roughly `1/sqrt(n-1)` and the quintile bucket size at `n/5`. "
        "This is the axis the bands differ on by construction.",
        "",
        "The cost charged in this grid is the evaluator's flat default, "
        "because these synthetic panels carry no per-row cost column. Real "
        "banded panels do carry one, and its per-band values are what actually "
        "sets the ratio above. This table cannot speak to that; it prices "
        "every band identically on purpose, so that what varies between bands "
        "here is cross-section size and nothing else.",
        "",
        "| regime | median annual vol | across-name log-sd | Student-t df |",
        "|---|---:|---:|---:|",
    ]
    for name in regimes:
        spec = regime_specs.get(name, {})
        lines.append(
            f"| `{name}` | {spec.get('annual_vol', float('nan')):.2f} "
            f"| {spec.get('annual_vol_log_sd', float('nan')):.2f} "
            f"| {spec.get('innovation_df', float('nan')):.1f} |"
        )
    lines += [
        "",
        "**These are declared brackets, not estimates.** Their direction and "
        "rough magnitude come from public stylised facts (Campbell, Lettau, "
        "Malkiel & Xu 2001 on idiosyncratic firm-level volatility and its "
        "spread; Cont 2001 on daily return tail indices of roughly 3-5 degrees "
        "of freedom). They are fitted to no archive, and no attempt is made to "
        f"claim they ARE the small-cap process. `{primary_regime}` is the "
        "primary; the others are run at the same cells so the regime's own "
        "contribution is measured below rather than assumed, and so a reader "
        "who thinks a different bracket is right can see how much the answer "
        "would move.",
        "",
        "## Provenance",
        "",
        f"- Grid: `{manifest['artifact']}` generated {manifest['created_utc']} "
        f"(generator commit `{manifest['code_commit']}`), 100% synthetic — no "
        "real, licensed or third-party market data. `synthetic_only` = "
        f"{manifest.get('synthetic_only')}.",
        f"- Grid request: `{grid_spec.get('kind', 'unknown')}`, spec sha256 "
        f"`{grid_spec.get('spec_sha256', 'n/a')}`, {grid_spec.get('n_cells', len(results))} "
        "cells.",
        f"- Evaluator: `stock-grader backtest` at Stock-Grader commit "
        f"`{current_commit()}` with `{' '.join(backtest_flags(periods_per_year))}`.",
        f"- `--periods-per-year {periods_per_year}` matches this grid's cadence. "
        "It cannot change a verdict: DSR runs on the unannualised per-period "
        "Sharpe, and the bootstrap scales every resampled Sharpe by a positive "
        "constant that cannot move the sign of the interval's lower bound. It "
        "is set correctly so the reported annualised Sharpes mean what they say.",
        "- One FRESH scratch ledger per replication, so DSR deflates a single "
        "trial (E[max] benchmark 0, DSR == PSR). The real programme charges "
        "its full nominal trial count, so real deflation is strictly harsher "
        "and true power is <= every number below.",
        "- Every input parquet was verified against the grid manifest's sha256 before evaluation.",
        "",
        "### Replication counts and how much noise they carry",
        "",
        "| cell kind | seeds | binomial SE at rate 0.5 | at rate 0.05 |",
        "|---|---:|---:|---:|",
    ]
    null_seeds = sorted({r.n_seeds for r in results if r.planted_ic == 0.0})
    signal_seeds = sorted({r.n_seeds for r in results if r.planted_ic > 0})
    for kind, seed_counts in (("planted IC = 0", null_seeds), ("planted IC > 0", signal_seeds)):
        for seeds in seed_counts:
            lines.append(
                f"| {kind} | {seeds} | {math.sqrt(0.25 / seeds):.3f} "
                f"| {math.sqrt(0.05 * 0.95 / seeds):.3f} |"
            )
    lines += [
        "",
        "A single cell is therefore worth roughly +/- 0.1 at mid rates. Read "
        "trends across a row, not one number.",
        "",
        "## False-positive rate at planted IC = 0",
        "",
        f"Fraction of null replications the gate passed (target: <= alpha = {GATE_ALPHA:.2f}).",
        "",
        "| band | cross-section | regime | periods | seeds | false-positive rate |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for result in sorted(
        (r for r in results if r.planted_ic == 0.0),
        key=lambda r: (r.label, r.regime, -r.months),
    ):
        lines.append(
            f"| {result.label} | {result.universe} | `{result.regime}` "
            f"| {result.months} | {result.n_seeds} | {result.gate_pass_rate:.2f} |"
        )

    for periods in period_axis:
        for regime in regimes:
            cells = [r for r in results if r.months == periods and r.regime == regime]
            if not any(r.planted_ic > 0 for r in cells):
                continue
            local_ics = sorted({r.planted_ic for r in cells if r.planted_ic > 0})
            lines += [
                "",
                f"## Detection power — {periods} periods, `{regime}`",
                "",
                "Fraction of replications the gate passed at each planted rank IC.",
                "",
                "| band | cross-section | "
                + " | ".join(f"IC {ic:.2f}" for ic in local_ics)
                + " | smallest detectable |",
                "|---|---:|" + "---:|" * len(local_ics) + "---|",
            ]
            for band in bands:
                selected = _select(cells, label=band, regime=regime, periods=periods)
                if not selected:
                    continue
                mdi = smallest_detectable_ic(selected)
                lines.append(
                    f"| {band} | {universe_of(band)} | "
                    + _power_row(selected, local_ics)
                    + f" | {_mdi_text(mdi)} |"
                )

    lines += [
        "",
        "## Gate anatomy — every cell",
        "",
        "`significant` requires BOTH DSR >= 0.95 AND bootstrap CI low > 0.",
        "",
        "| band | regime | periods | universe | planted IC | seeds | mean realized IC "
        "| DSR >= 0.95 | CI low > 0 | gate |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(results, key=lambda r: (r.label, r.regime, -r.months, r.planted_ic)):
        lines.append(
            f"| {r.label} | `{r.regime}` | {r.months} | {r.universe} "
            f"| {r.planted_ic:.2f} | {r.n_seeds} | {r.mean_realized_rank_ic:+.4f} "
            f"| {_rate(r.dsr_pass_rate)} | {_rate(r.ci_low_positive_rate)} "
            f"| {_rate(r.gate_pass_rate)} |"
        )

    lines += ["", "## What a null result in each band does and does not prove", ""]
    lines += _band_readings(
        results,
        primary_regime=primary_regime,
        primary_periods=primary_periods,
        universe_of=universe_of,
        bands=bands,
    )
    lines += ["", "## Regime sensitivity", ""]
    lines += _regime_section(results, regime_specs)
    lines += ["", "## Standing caveats", "", _band_caveats(primary_periods)]
    return "\n".join(lines) + "\n"


def _band_readings(
    results: list[CellResult],
    *,
    primary_regime: str,
    primary_periods: int,
    universe_of: Callable[[str], int | None],
    bands: list[str],
) -> list[str]:
    """One honest paragraph per band, computed from that band's own cells."""

    lines: list[str] = []
    for band in bands:
        cells = _select(results, label=band, regime=primary_regime, periods=primary_periods)
        if not cells:
            continue
        mdi = smallest_detectable_ic(cells)
        null = next((c for c in cells if c.planted_ic == 0.0), None)
        signal = sorted((c for c in cells if c.planted_ic > 0), key=lambda c: c.planted_ic)
        undetected = [c.planted_ic for c in signal if c.gate_pass_rate < DETECTION_THRESHOLD]
        largest_missed = max(undetected) if undetected else None
        lines += [
            f"### Band {band} — {universe_of(band)} names per period, "
            f"{primary_periods} periods, `{primary_regime}`",
            "",
            f"- False-positive rate at planted IC = 0: "
            f"**{null.gate_pass_rate:.2f}** over {null.n_seeds} replications"
            if null
            else "- No null cell in this grid for this band.",
            f"- Smallest detectable planted rank IC (>= "
            f"{DETECTION_THRESHOLD:.0%} of replications passing the gate): "
            f"**{_mdi_text(mdi)}**.",
        ]
        if mdi is None:
            lines.append(
                f"- **A null result in band {band} proves nothing about ICs up "
                f"to {max((c.planted_ic for c in signal), default=0.0):.2f}.** "
                "The gate did not reach the detection threshold at ANY planted "
                "IC in the tested range at this sample size, so 'no edge' and "
                "'an edge as large as the largest value tested' are "
                "indistinguishable here. Report the band as UNDERPOWERED, not "
                "as evidence against an edge."
            )
        else:
            lines.append(
                f"- **A null result in band {band} rules out a persistent rank "
                f"IC at or above {mdi:.2f}, and nothing smaller.**"
                + (
                    f" In particular it does not rule out an IC of "
                    f"{largest_missed:.2f}, which this gate missed more than "
                    f"half the time at this sample size."
                    if largest_missed is not None
                    else ""
                )
            )
        lines += [
            f"- Because these replications are single-trial deflated and the "
            f"real programme charges its full nominal trial count, band {band}'s "
            "true detectable IC is LARGER than the number above, not smaller.",
            "",
        ]
    return lines[:-1] if lines and lines[-1] == "" else lines


def _regime_pair_gap(
    results: Sequence[CellResult], base: str, other: str
) -> tuple[int, float, float] | None:
    """Mean and largest gate-pass gap between two regimes over shared cells.

    Returns ``(n_shared_cells, mean_gap, largest_gap)`` where a gap is
    ``other - base``, or ``None`` when the two regimes share no signal cell.
    """

    keyed = {(r.label, r.months, r.planted_ic, r.regime): r for r in results if r.regime}
    gaps = [
        keyed[(label, periods, ic, other)].gate_pass_rate - result.gate_pass_rate
        for (label, periods, ic, regime), result in keyed.items()
        if regime == base and ic > 0 and (label, periods, ic, other) in keyed
    ]
    if not gaps:
        return None
    return len(gaps), sum(gaps) / len(gaps), max(gaps, key=abs)


def _regime_section(results: Sequence[CellResult], regime_specs: dict) -> list[str]:
    """Decompose the regime effect over every ordered pair the grid supports."""

    regimes = sorted({r.regime for r in results if r.regime})
    if len(regimes) < 2:
        return [
            "Only one regime was run, so this grid cannot say how much of its "
            "answer comes from the return process. Read every number as "
            "conditional on that single declared bracket."
        ]

    def axis(base: str, other: str) -> str:
        """Name the axes on which two regimes actually differ."""
        left, right = regime_specs.get(base, {}), regime_specs.get(other, {})
        moved = [
            label
            for label, key in (
                ("level", "annual_vol"),
                ("across-name dispersion", "annual_vol_log_sd"),
                ("tail index", "innovation_df"),
            )
            if left.get(key) != right.get(key)
        ]
        return " + ".join(moved) if moved else "nothing"

    lines = [
        "The regimes are compared pairwise so the volatility LEVEL (which "
        "enters only through the fixed-cost ratio described above) is "
        "separated from the scale-free shape terms. A gap is the second "
        "regime's gate-pass rate minus the first's, averaged over every "
        "planted-signal cell the two share.",
        "",
        "| from | to | axes that move | shared cells | mean gap | largest gap |",
        "|---|---|---|---:|---:|---:|",
    ]
    reported: list[tuple[str, str, float]] = []
    for index, base in enumerate(regimes):
        for other in regimes[index + 1 :]:
            gap = _regime_pair_gap(results, base, other)
            if gap is None:
                continue
            count, mean_gap, largest = gap
            lines.append(
                f"| `{base}` | `{other}` | {axis(base, other)} | {count} "
                f"| {mean_gap:+.2f} | {largest:+.2f} |"
            )
            reported.append((base, other, mean_gap))
    if not reported:
        lines = [
            "The grid's regimes share no planted-signal cell, so no pairwise "
            "comparison is possible and the regime's contribution cannot be "
            "quantified from this artifact."
        ]
        return lines
    biggest = max(reported, key=lambda row: abs(row[2]))
    lines += [
        "",
        f"The largest single effect is `{biggest[0]}` to `{biggest[1]}` at "
        f"{biggest[2]:+.2f} mean gate-pass rate. A gap that size is the price "
        "of the regime assumption, and it is why the regime is declared in "
        "advance and reported rather than chosen after the fact. Gaps under "
        "about 0.1 are inside the replication noise quoted above and are not "
        "evidence of a regime effect.",
    ]
    return lines


def _band_caveats(primary_periods: int) -> str:
    return "\n".join(
        [
            f"- Every number here is an UPPER bound on real power at "
            f"{primary_periods} periods, for three independent reasons: "
            "single-trial deflation (above); a synthetic world with no "
            "delistings, no missing rows and a cross-section whose membership "
            "never changes; and a flat cost charge that is the same in every "
            "band when the real charge is not.",
            "- The cost point is the one most likely to be misread, so once "
            "more: the gate's returns are net of a deduction in return units, "
            "so a band modelled as more volatile at a fixed charge looks "
            "EASIER to detect in than it is. Real smaller-cap bands carry "
            "larger costs, which pushes the other way. This table holds the "
            "charge flat across bands deliberately, so the only thing varying "
            "between bands here is cross-section size — but that means it "
            "understates how much harder the small bands really are.",
            "- The planted signal is STATIONARY: the same rank IC in every "
            "period. A real signal that decays, or works only in some regimes, "
            "is harder to detect than anything tabulated here.",
            "- Cross-sections are independent draws across bands. Real bands "
            "are disjoint slices of the same dates and share a market factor, "
            "which this grid does not model; band-to-band comparisons of power "
            "are therefore cleaner here than they will be in practice.",
            "- The gate's structural floor still applies: "
            "`block_bootstrap_sharpe_ci` returns (0, 0) below 11 periods, so "
            "`significant` cannot be true there at any signal strength, and "
            "`assess_edge` reports INSUFFICIENT SAMPLE below 30. That was "
            "established by `power_table_2026-08-03` and is not re-derived "
            "here.",
            "- A band's power depends on its cross-section size, which drifts "
            "as the archive grows. These numbers describe the sizes named in "
            "each row and no others.",
        ]
    )


def render_markdown(manifest: dict, results: list[CellResult], *, date_tag: str) -> str:
    months_axis = sorted({r.months for r in results})
    universes_axis = sorted({r.universe for r in results})
    ics_axis = sorted({r.planted_ic for r in results})
    positive_ics = [ic for ic in ics_axis if ic > 0]

    lines: list[str] = [
        f"# Planted-IC power table — {date_tag}",
        "",
        "Calibration of the monthly forward-backtest significance gate "
        "(`stock-grader backtest`: PSR/DSR + bootstrap Sharpe CI, "
        "`significant` = DSR >= 0.95 AND CI low > 0) against fully synthetic "
        "panels with a KNOWN planted cross-sectional rank IC.",
        "",
        "## Provenance",
        "",
        f"- Grid: `{manifest['artifact']}` generated {manifest['created_utc']} "
        f"(generator commit `{manifest['code_commit']}`), 100% synthetic "
        "(no real or licensed market data; the raw grid stays in its source repo).",
        f"- Evaluator: `stock-grader backtest` at Stock-Grader commit `{current_commit()}` "
        "with the exact monthly-forward-backtest.yml flags: "
        f"`{' '.join(PRODUCTION_BACKTEST_FLAGS)}`.",
        "- One fresh scratch ledger per replication: DSR deflates a single trial "
        "(E[max] benchmark 0, so DSR == PSR). The production ledger accumulates "
        "trials monthly, so production deflation is at least as harsh and true "
        "power is <= every number below.",
        "- Every input parquet was verified against the grid manifest's sha256 before evaluation.",
        "- Sampling noise: most cells use 20 replications (100 for the 3/6-month "
        "null cells), so a tabulated rate carries a binomial standard error of "
        "up to ~0.11. Adjacent cells can invert — e.g. planted 0.02 vs 0.03 at "
        "12 months / 250 names, where the grid's realized mean rank ICs "
        "themselves inverted (0.026 vs 0.024 per its manifest). Read trends, "
        "not single cells.",
        "",
        "## False-positive rate at planted IC = 0",
        "",
        f"Fraction of null replications the gate passed (target: <= alpha = {GATE_ALPHA:.2f}).",
        "",
        "| months | " + " | ".join(f"universe {u}" for u in universes_axis) + " | seeds |",
        "|---|" + "---|" * (len(universes_axis) + 1),
    ]
    by_key = {(r.planted_ic, r.months, r.universe): r for r in results}
    for months in months_axis:
        cells = [by_key.get((0.0, months, u)) for u in universes_axis]
        seeds = {c.n_seeds for c in cells if c}
        lines.append(
            f"| {months} | "
            + " | ".join(_rate(c.gate_pass_rate if c else None) for c in cells)
            + f" | {'/'.join(str(s) for s in sorted(seeds))} |"
        )

    for universe in universes_axis:
        lines += [
            "",
            f"## Detection power, universe {universe}",
            "",
            "Fraction of replications the gate passed at each planted rank IC.",
            "",
            *_grid_table(
                results, universe=universe, months_axis=months_axis, ics_axis=positive_ics
            ),
        ]

    lines += [
        "",
        "## Gate anatomy",
        "",
        "`significant` requires BOTH DSR >= 0.95 AND bootstrap CI low > 0. "
        "The bootstrap CI (`block_bootstrap_sharpe_ci`, block = 10) returns "
        "(0, 0) whenever there are fewer than 11 periods, so at 3 and 6 months "
        "the CI-low condition can NEVER hold and the gate is structurally "
        "closed regardless of signal strength. Below 30 periods the verdict "
        "string additionally reads INSUFFICIENT SAMPLE.",
        "",
        "| months | universe | planted IC | DSR >= 0.95 rate | CI low > 0 rate | gate rate |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (r.months, r.universe, r.planted_ic)):
        lines.append(
            f"| {r.months} | {r.universe} | {r.planted_ic:.2f} "
            f"| {_rate(r.dsr_pass_rate)} | {_rate(r.ci_low_positive_rate)} "
            f"| {_rate(r.gate_pass_rate)} |"
        )

    lines += ["", "## How to read the September 2026 verdicts", "", _honest_paragraph(results), ""]
    return "\n".join(lines)


def _honest_paragraph(results: list[CellResult]) -> str:
    """One paragraph, computed from the actual numbers, on what the gate can see."""

    by_key = {(r.planted_ic, r.months, r.universe): r for r in results}

    def smallest_detectable(months: int, universe: int, threshold: float = 0.5) -> str:
        candidates = sorted(
            r.planted_ic
            for r in results
            if r.months == months
            and r.universe == universe
            and r.planted_ic > 0
            and r.gate_pass_rate >= threshold
        )
        return f"{candidates[0]:.2f}" if candidates else "none in the tested grid (max 0.05)"

    fp_cells = [r for r in results if r.planted_ic == 0.0]
    max_fp = max((r.gate_pass_rate for r in fp_cells), default=0.0)
    return (
        "At 3 and 6 matured periods the gate cannot pass at all — the bootstrap "
        "CI is undefined below 11 periods and returns (0, 0), so `significant` "
        "is structurally false; a September 2026 verdict of NO EDGE or "
        "INSUFFICIENT SAMPLE at ~3 periods is therefore NOT evidence that the "
        "scores lack skill (under-reading risk: do not kill a profile on it). "
        f"With >= 50% detection probability as the bar, the smallest detectable "
        f"planted rank IC is {smallest_detectable(12, 1000)} at 12 months and "
        f"{smallest_detectable(36, 1000)} at 36 months for a 1000-name universe "
        f"({smallest_detectable(12, 250)} and {smallest_detectable(36, 250)} "
        "for 250 names). Conversely, the worst observed false-positive rate at "
        f"planted IC = 0 was {max_fp:.2f} across every null cell, and these "
        "numbers are an UPPER bound on production power because each replication "
        "here is deflated as a single-trial ledger while the real ledger's "
        "multi-trial E[max] benchmark only rises (over-reading risk: a future "
        "PASS at 12+ periods on a small universe should still be checked against "
        "this table's power at the realized IC, not celebrated as proof of a "
        "large edge — a gate this underpowered mostly passes lucky draws of "
        "genuinely large signals)."
    )


def write_artifacts(
    out_dir: Path,
    date_tag: str,
    manifest: dict,
    results: list[CellResult],
    *,
    stem: str = "power_table",
    periods_per_year: int = 12,
    primary_regime: str = "",
    primary_periods: int | None = None,
) -> list[Path]:
    """Write the dated, immutable md/json/sha256 artifact trio.

    A grid whose cells carry band labels renders the banded report; anything
    else renders exactly the report the 2026-08-03 artifact was written with,
    so that table stays reproducible from this script.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}_{date_tag}.md"
    json_path = out_dir / f"{stem}_{date_tag}.json"
    sha_path = out_dir / f"{stem}_{date_tag}.sha256"
    for path in (md_path, json_path, sha_path):
        if path.exists():
            raise FileExistsError(
                f"{path} already exists; dated artifacts are immutable — "
                "use a new date tag instead of overwriting"
            )
    banded = any(result.label for result in results)
    if banded:
        primary_regime = primary_regime or _majority_regime(results)
        primary_periods = (
            primary_periods
            if primary_periods is not None
            else max(result.months for result in results)
        )
        body = render_band_markdown(
            manifest,
            results,
            date_tag=date_tag,
            periods_per_year=periods_per_year,
            primary_regime=primary_regime,
            primary_periods=primary_periods,
        )
    else:
        body = render_markdown(manifest, results, date_tag=date_tag)
    payload = {
        "schema_version": "stock_grader.power_table/2"
        if banded
        else ("stock_grader.power_table/1"),
        "date": date_tag,
        "evaluator": {
            "command": "stock-grader backtest",
            "flags": list(backtest_flags(periods_per_year)),
            "gate": "significant = DSR >= 0.95 and bootstrap sharpe CI low > 0",
            "ledger_policy": "one fresh scratch ledger per replication (DSR == PSR)",
            "code_commit": current_commit(),
        },
        "grid_provenance": {
            "artifact": manifest["artifact"],
            "created_utc": manifest["created_utc"],
            "generator_commit": manifest["code_commit"],
            "synthetic_only": manifest["synthetic_only"],
            "grid": manifest.get("grid", {}),
            "regimes": manifest.get("generator", {}).get("regimes", {}),
        },
        "cells": [asdict(result) for result in results],
    }
    if banded:
        payload["reading"] = {
            "primary_regime": primary_regime,
            "primary_periods": primary_periods,
            "detection_threshold": DETECTION_THRESHOLD,
            "smallest_detectable_ic": {
                band: smallest_detectable_ic(
                    _select(
                        results,
                        label=band,
                        regime=primary_regime,
                        periods=int(primary_periods or 0),
                    )
                )
                for band in sorted({r.label for r in results if r.label})
            },
            "false_positive_rate": {
                band: next(
                    (
                        r.gate_pass_rate
                        for r in _select(
                            results,
                            label=band,
                            regime=primary_regime,
                            periods=int(primary_periods or 0),
                        )
                        if r.planted_ic == 0.0
                    ),
                    None,
                )
                for band in sorted({r.label for r in results if r.label})
            },
        }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    md_path.write_text(body)
    sha_path.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in (json_path, md_path))
    )
    return [md_path, json_path, sha_path]


def _majority_regime(results: Sequence[CellResult]) -> str:
    counts: dict[str, int] = {}
    for result in results:
        if result.regime:
            counts[result.regime] = counts.get(result.regime, 0) + 1
    return max(counts, key=lambda name: counts[name]) if counts else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--grid-dir", required=True, type=Path, help="calibration grid with manifest.json"
    )
    parser.add_argument(
        "--scratch-dir",
        required=True,
        type=Path,
        help="workspace OUTSIDE the repo for per-seed panels and throwaway ledgers",
    )
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "docs" / "calibration")
    parser.add_argument(
        "--date",
        default=None,
        help="artifact date tag (default: the grid directory's basename)",
    )
    parser.add_argument(
        "--stem",
        default="power_table",
        help="artifact file stem (default: %(default)s)",
    )
    parser.add_argument(
        "--periods-per-year",
        type=int,
        default=12,
        help=(
            "annualisation matching the grid's cadence (24 for twice-monthly). "
            "Cannot change a gate verdict; see backtest_flags."
        ),
    )
    parser.add_argument(
        "--primary-regime",
        default="",
        help="regime the per-band reading is written against (default: the most common)",
    )
    parser.add_argument(
        "--primary-periods",
        type=int,
        default=None,
        help="period count the per-band reading is written against (default: the largest)",
    )
    parser.add_argument("--no-verify-hashes", action="store_true")
    args = parser.parse_args(argv)

    date_tag = args.date or args.grid_dir.resolve().name
    manifest, results = run_grid(
        args.grid_dir,
        args.scratch_dir,
        verify_hashes=not args.no_verify_hashes,
        periods_per_year=args.periods_per_year,
    )
    paths = write_artifacts(
        args.out_dir,
        date_tag,
        manifest,
        results,
        stem=args.stem,
        periods_per_year=args.periods_per_year,
        primary_regime=args.primary_regime,
        primary_periods=args.primary_periods,
    )
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
