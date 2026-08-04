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
    """Aggregate gate behaviour for one (planted IC, months, universe) cell."""

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


def evaluate_seed_panel(panel: pd.DataFrame, workdir: Path, label: str) -> dict:
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
                *PRODUCTION_BACKTEST_FLAGS,
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


def evaluate_cell_frame(frame: pd.DataFrame, workdir: Path, label: str) -> list[SeedOutcome]:
    """Split a stacked cell by ``seed`` and evaluate each replication."""

    outcomes: list[SeedOutcome] = []
    for seed_value, group in frame.groupby("seed", sort=True):
        panel = group.drop(columns=["seed"]).reset_index(drop=True)
        payload = evaluate_seed_panel(panel, workdir, f"{label}_s{int(seed_value):04d}")
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
        insufficient_sample_verdicts=sum(
            "INSUFFICIENT SAMPLE" in o.verdict for o in outcomes
        ),
    )


def run_grid(
    grid_dir: Path,
    scratch_dir: Path,
    *,
    verify_hashes: bool = True,
    progress: bool = True,
) -> tuple[dict, list[CellResult]]:
    """Evaluate every cell of a calibration grid; returns (manifest, results)."""

    assert_outside_repo(scratch_dir, what="scratch directory")
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
        outcomes = evaluate_cell_frame(frame, scratch_dir / label, label)
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
        "- Every input parquet was verified against the grid manifest's sha256 "
        "before evaluation.",
        "- Sampling noise: most cells use 20 replications (100 for the 3/6-month "
        "null cells), so a tabulated rate carries a binomial standard error of "
        "up to ~0.11. Adjacent cells can invert — e.g. planted 0.02 vs 0.03 at "
        "12 months / 250 names, where the grid's realized mean rank ICs "
        "themselves inverted (0.026 vs 0.024 per its manifest). Read trends, "
        "not single cells.",
        "",
        "## False-positive rate at planted IC = 0",
        "",
        "Fraction of null replications the gate passed "
        f"(target: <= alpha = {GATE_ALPHA:.2f}).",
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
            if r.months == months and r.universe == universe and r.planted_ic > 0
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
    out_dir: Path, date_tag: str, manifest: dict, results: list[CellResult]
) -> list[Path]:
    """Write the dated, immutable md/json/sha256 artifact trio."""

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"power_table_{date_tag}.md"
    json_path = out_dir / f"power_table_{date_tag}.json"
    sha_path = out_dir / f"power_table_{date_tag}.sha256"
    for path in (md_path, json_path, sha_path):
        if path.exists():
            raise FileExistsError(
                f"{path} already exists; dated artifacts are immutable — "
                "use a new date tag instead of overwriting"
            )
    payload = {
        "schema_version": "stock_grader.power_table/1",
        "date": date_tag,
        "evaluator": {
            "command": "stock-grader backtest",
            "flags": list(PRODUCTION_BACKTEST_FLAGS),
            "gate": "significant = DSR >= 0.95 and bootstrap sharpe CI low > 0",
            "ledger_policy": "one fresh scratch ledger per replication (DSR == PSR)",
            "code_commit": current_commit(),
        },
        "grid_provenance": {
            "artifact": manifest["artifact"],
            "created_utc": manifest["created_utc"],
            "generator_commit": manifest["code_commit"],
            "synthetic_only": manifest["synthetic_only"],
        },
        "cells": [asdict(result) for result in results],
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    md_path.write_text(render_markdown(manifest, results, date_tag=date_tag))
    sha_path.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in (json_path, md_path))
    )
    return [md_path, json_path, sha_path]


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
    parser.add_argument("--no-verify-hashes", action="store_true")
    args = parser.parse_args(argv)

    date_tag = args.date or args.grid_dir.resolve().name
    manifest, results = run_grid(
        args.grid_dir,
        args.scratch_dir,
        verify_hashes=not args.no_verify_hashes,
    )
    paths = write_artifacts(args.out_dir, date_tag, manifest, results)
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
