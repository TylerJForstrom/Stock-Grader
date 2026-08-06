"""Rendering: terminal, JSON, and Markdown.

The terminal report is the primary interface and is built around one principle — **a grade that
cannot be argued with is not useful**. So every report shows its model-sensitivity interval beside
the score, the data coverage behind it, which attributes pushed the grade up and down, and any
warning that qualifies it (synthetic prices, missing pillars, a weighting method that fell back).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd
from rich.box import SIMPLE
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .types import GradeReport

__all__ = [
    "rank_reports",
    "render_consensus",
    "render_ranking",
    "render_report",
    "to_consensus_markdown",
    "to_json",
    "to_markdown",
    "to_ranking_markdown",
]

# Colour by letter family, so a grade reads at a glance.
_COLOURS = {"A": "bright_green", "B": "green", "C": "yellow", "D": "dark_orange", "F": "red"}


def _colour(letter: str) -> str:
    return _COLOURS.get(letter[:1], "white")


def _bar(score: float, width: int = 24) -> Text:
    """Horizontal bar for a 0..100 score."""
    if math.isnan(score):
        return Text("  n/a", style="dim")
    filled = int(round(max(0.0, min(100.0, score)) / 100.0 * width))
    text = Text()
    text.append("█" * filled, style=_colour(_letter_for(score)))
    text.append("░" * (width - filled), style="dim")
    return text


def _letter_for(score: float) -> str:
    from .scoring import to_letter

    return to_letter(score)


def _has_sensitivity_interval(report: GradeReport) -> bool:
    interval = report.sensitivity_interval
    return bool(interval and all(math.isfinite(float(value)) for value in interval))


def _letter_scenario_frequency_text(report: GradeReport) -> str:
    finite = {
        letter: probability
        for letter, probability in report.letter_probabilities.items()
        if math.isfinite(probability) and probability >= 0.0
    }
    ordered = sorted(finite.items(), key=lambda item: (-item[1], item[0]))
    return "  ·  ".join(f"{letter} {probability:.0%}" for letter, probability in ordered)


def rank_reports(
    reports: dict[str, GradeReport],
    *,
    top: int | None = None,
) -> list[GradeReport]:
    """Return the canonical rank order used by terminal, Markdown, and JSON output."""
    ordered = sorted(
        reports.values(),
        key=lambda report: (
            not report.graded,
            -report.score if math.isfinite(report.score) else 1e9,
            report.ticker,
        ),
    )
    return ordered if top is None else ordered[: max(0, top)]


# §8: every surface that shows a grade carries this. A-to-F letters read as
# recommendations; they are peer-relative research screens and must say so.
DISCLAIMER = (
    "Peer-relative research screen from historical filings - not investment "
    "advice, not a prediction of returns."
)


def render_report(
    report: GradeReport, console: Console | None = None, *, explain: bool = False
) -> None:
    """Render one security's grade to the terminal."""
    console = console or Console()

    header = Text()
    header.append(f"{report.ticker}  ", style="bold white")
    header.append(f"{report.letter}", style=f"bold {_colour(report.letter)}")
    header.append(f"  {report.score:.1f}/100", style="white")
    if _has_sensitivity_interval(report):
        low, high = report.sensitivity_interval or (float("nan"), float("nan"))
        header.append(f"   90% sensitivity [{low:.0f}–{high:.0f}]", style="dim")
    if report.percentile is not None:
        header.append(f"   {report.percentile:.0f}th pct", style="dim")

    meta = Text()
    meta.append(f"profile {report.profile}", style="cyan")
    meta.append(f"  ·  weighting {report.weighting_method}", style="dim")
    meta.append(f"  ·  norm {report.normalizer}", style="dim")
    meta.append(f"  ·  agg {report.aggregator}", style="dim")
    meta.append(f"\ncoverage {report.coverage:.0%}", style="dim")
    if report.lost_weight > 0.02:
        meta.append(f"  ·  {report.lost_weight:.0%} of nominal weight inert", style="yellow")
    meta.append(
        f"  ({report.explain.get('n_metrics_ok', 0)} computed, "
        f"{report.explain.get('n_metrics_missing', 0)} missing, "
        f"{report.explain.get('n_metrics_not_applicable', 0)} n/a for sector)",
        style="dim",
    )
    if _has_sensitivity_interval(report):
        meta.append(
            "\nmodel sensitivity perturbs weights and metric inclusion; it is not a return forecast",
            style="dim",
        )
    frequencies = _letter_scenario_frequency_text(report)
    if frequencies:
        meta.append(f"\nletter scenario frequencies  {frequencies}", style="dim")

    pillars = Table(box=SIMPLE, show_header=True, header_style="bold dim", pad_edge=False)
    pillars.add_column("pillar", style="white", width=14)
    pillars.add_column("score", justify="right", width=6)
    pillars.add_column("", width=26)
    pillars.add_column("weight", justify="right", width=7)
    pillars.add_column("eff", justify="right", width=6)
    pillars.add_column("contrib", justify="right", width=8)
    contributions = report.explain.get("pillar_contributions", {})
    for name, pillar in sorted(report.pillars.items(), key=lambda kv: -kv[1].score):
        contribution = contributions.get(name, 0.0)
        pillars.add_row(
            name,
            f"{pillar.score:.1f}",
            _bar(pillar.score),
            f"{report.pillar_weights.get(name, 0.0):.0%}",
            f"{report.effective_pillar_weights.get(name, 0.0):.0%}",
            Text(f"{contribution:+.2f}", style="green" if contribution >= 0 else "red"),
        )

    blocks: list[Any] = [header, meta, pillars]

    if explain:
        drivers = Table(box=SIMPLE, show_header=True, header_style="bold dim", pad_edge=False)
        drivers.add_column("strongest", style="green", width=30)
        drivers.add_column("", justify="right", width=8)
        drivers.add_column("weakest", style="red", width=30)
        drivers.add_column("", justify="right", width=8)
        best = report.top_contributors(6)
        worst = report.top_contributors(6, positive=False)
        for i in range(max(len(best), len(worst))):
            b = best[i] if i < len(best) else ("", 0.0)
            w = worst[i] if i < len(worst) else ("", 0.0)
            drivers.add_row(
                b[0],
                f"{b[1]:+.2f}" if b[0] else "",
                w[0],
                f"{w[1]:+.2f}" if w[0] else "",
            )
        blocks.append(drivers)

    if report.warnings:
        warn = Text()
        for message in report.warnings[:8]:
            warn.append(f"! {message}\n", style="yellow")
        blocks.append(warn)

    console.print(
        Panel(
            Group(*blocks),
            border_style=_colour(report.letter),
            title="stock-grader",
            title_align="left",
        )
    )
    console.print(Text(DISCLAIMER, style="dim"))


def render_ranking(
    reports: dict[str, GradeReport], console: Console | None = None, *, top: int | None = None
) -> None:
    """Render a ranked universe."""
    console = console or Console()
    table = Table(box=SIMPLE, header_style="bold", title="Ranked universe", title_justify="left")
    table.add_column("#", justify="right", width=4, style="dim")
    table.add_column("ticker", style="bold")
    table.add_column("grade", justify="center")
    table.add_column("score", justify="right")
    table.add_column("", width=22)
    table.add_column("90% sensitivity", justify="center", style="dim")
    table.add_column("cov", justify="right", style="dim")
    table.add_column("sector", style="dim")

    # Ungraded names sort last regardless of score: an N/A at the top of a ranked list reads as
    # "best", when it means the opposite — we declined to judge it.
    ordered = rank_reports(reports, top=top)
    for i, report in enumerate(ordered, 1):
        interval = report.sensitivity_interval
        sensitivity = (
            f"{interval[0]:.0f}–{interval[1]:.0f}"
            if _has_sensitivity_interval(report) and interval is not None
            else "—"
        )
        table.add_row(
            str(i),
            report.ticker,
            Text(report.letter, style=f"bold {_colour(report.letter)}"),
            f"{report.score:.1f}",
            _bar(report.score, 20),
            sensitivity,
            f"{report.coverage:.0%}",
            str(report.meta.get("sector", "")),
        )
    console.print(table)
    console.print(Text(DISCLAIMER, style="dim"))


def _letter_distribution_text(result) -> str:
    """ "3xB+ 5xB 2xC" — the count of graded profiles at each letter."""
    if not getattr(result, "letter_distribution", None):
        return "—"
    order = {
        letter: index
        for index, letter in enumerate(
            ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"]
        )
    }
    items = sorted(result.letter_distribution.items(), key=lambda kv: order.get(kv[0], 99))
    return " ".join(f"{count}x{letter}" for letter, count in items)


def render_consensus(results: dict, console: Console | None = None) -> None:
    """Render the multi-profile consensus, foregrounding disagreement."""
    console = console or Console()
    table = Table(
        box=SIMPLE,
        header_style="bold",
        title="Consensus across profiles — a contested letter is information, not noise",
        title_justify="left",
    )
    table.add_column("ticker", style="bold")
    table.add_column("consensus", justify="center")
    table.add_column("score", justify="right")
    table.add_column("letters", justify="left")
    table.add_column("best as", style="green")
    table.add_column("worst as", style="red")

    for result in sorted(
        results.values(), key=lambda r: -r.score if not math.isnan(r.score) else 1e9
    ):
        if not len(result.scores):
            # Never drop a requested security silently. A ticker that vanishes from the output
            # reads as "not asked for"; the honest answer is that we could not grade it and why.
            table.add_row(
                result.ticker,
                Text("N/A", style="dim"),
                "—",
                "—",
                Text("could not be graded — too little filing history", style="dim"),
                "",
            )
            continue
        distribution = _letter_distribution_text(result)
        contested = len(result.letter_distribution) > 2
        table.add_row(
            result.ticker,
            Text(result.letter, style=f"bold {_colour(result.letter)}"),
            f"{result.score:.1f}",
            Text(distribution, style="yellow" if contested else "dim"),
            f"{result.best_profile} ({result.scores.max():.0f})",
            f"{result.worst_profile} ({result.scores.min():.0f})",
        )
    console.print(table)
    console.print(Text(DISCLAIMER, style="dim"))


def _encode(value: Any) -> Any:
    if isinstance(value, GradeReport):
        encoded = asdict(value)
        # ``ci`` remains present for compatibility; the additional name states what it measures.
        encoded["sensitivity_interval"] = value.sensitivity_interval
        encoded["letter_probabilities"] = value.letter_probabilities
        return _encode(encoded)
    if value is pd.NA:
        return None
    if isinstance(value, np.generic):
        return _encode(value.item())
    if isinstance(value, np.ndarray):
        return [_encode(item) for item in value.tolist()]
    if isinstance(value, pd.Series):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, pd.DataFrame):
        return _encode(value.to_dict(orient="index"))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _encode(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _encode(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_encode(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def to_json(reports: Any, *, indent: int = 2) -> str:
    """Stable JSON representation, suitable for piping into other tools.

    The disclaimer lives INSIDE each report's meta (schema-additive) rather
    than as an envelope: consumers parse this output as the data itself and an
    envelope would break the documented contract.
    """
    return json.dumps(_encode(reports), indent=indent, allow_nan=False, sort_keys=True)


def _provenance_line(report: GradeReport) -> str:
    """One line that makes a pasted report self-identifying.

    A markdown grade circulates detached from the run that produced it, and two
    scores are comparable only when their config and universe fingerprints match
    (ECOSYSTEM rule 3). Without this header a stale pasted report is
    indistinguishable from a current one.
    """
    config_fp = str(report.meta.get("config_fingerprint", ""))[:12] or "unknown"
    universe_fp = str(report.meta.get("universe_fingerprint", ""))[:12] or "unknown"
    return (
        f"- **As of**: {report.asof or 'unknown'}   "
        f"**Config**: `{config_fp}`   **Universe**: `{universe_fp}`"
    )


def to_markdown(report: GradeReport) -> str:
    """Markdown report for pasting into notes or a PR."""
    lines = [
        f"# {report.ticker} — {report.letter} ({report.score:.1f}/100)",
        "",
        f"> {DISCLAIMER}",
        "",
        f"- **Profile**: {report.profile}",
        _provenance_line(report),
        (
            f"- **Weighting**: {report.weighting_method}   **Normalizer**: {report.normalizer}   "
            + f"**Aggregator**: {report.aggregator}"
        ),
        (
            f"- **Coverage**: {report.coverage:.0%} "
            + f"({report.explain.get('n_metrics_ok', 0)} computed, "
            + f"{report.explain.get('n_metrics_missing', 0)} missing, "
            + f"{report.explain.get('n_metrics_not_applicable', 0)} not applicable)"
        ),
    ]
    if _has_sensitivity_interval(report):
        low, high = report.sensitivity_interval or (float("nan"), float("nan"))
        lines.append(
            f"- **90% model sensitivity interval**: {low:.1f} – {high:.1f} "
            "(weight and metric-inclusion perturbations; not a return forecast)"
        )
    frequencies = _letter_scenario_frequency_text(report)
    if frequencies:
        lines.append(f"- **Letter frequencies under those model perturbations**: {frequencies}")
    if report.percentile is not None:
        lines.append(f"- **Universe percentile**: {report.percentile:.0f}")
    lines += [
        "",
        "## Pillars",
        "",
        "| pillar | score | weight | contribution |",
        "|---|---:|---:|---:|",
    ]
    contributions = report.explain.get("pillar_contributions", {})
    for name, pillar in sorted(report.pillars.items(), key=lambda kv: -kv[1].score):
        lines.append(
            f"| {name} | {pillar.score:.1f} | {report.pillar_weights.get(name, 0.0):.0%} "
            f"| {contributions.get(name, 0.0):+.2f} |"
        )
    lines += ["", "## Strongest attributes", "", "| metric | contribution |", "|---|---:|"]
    for name, value in report.top_contributors(8):
        lines.append(f"| {name} | {value:+.3f} |")
    lines += ["", "## Weakest attributes", "", "| metric | contribution |", "|---|---:|"]
    for name, value in report.top_contributors(8, positive=False):
        lines.append(f"| {name} | {value:+.3f} |")
    if report.warnings:
        lines += ["", "## Caveats", ""]
        lines += [f"- {w}" for w in report.warnings]
    return "\n".join(lines)


def to_ranking_markdown(
    reports: dict[str, GradeReport],
    *,
    top: int | None = None,
) -> str:
    """Compact ranked-universe table, using the same ordering as terminal and JSON."""
    lines = ["# Ranked universe", "", f"> {DISCLAIMER}", ""]
    ordered_reports = rank_reports(reports, top=top)
    if ordered_reports:
        # One run, one config/universe pair: any report's provenance is the table's.
        lines += [_provenance_line(ordered_reports[0]), ""]
    lines += [
        "| # | ticker | grade | score | 90% model sensitivity | coverage | sector |",
        "|---:|---|:---:|---:|:---:|---:|---|",
    ]
    for position, report in enumerate(ordered_reports, 1):
        interval = report.sensitivity_interval
        sensitivity = (
            f"{interval[0]:.0f}–{interval[1]:.0f}"
            if _has_sensitivity_interval(report) and interval is not None
            else "—"
        )
        lines.append(
            f"| {position} | {report.ticker} | {report.letter} | {report.score:.1f} "
            f"| {sensitivity} | {report.coverage:.0%} "
            f"| {report.meta.get('sector', '')} |"
        )
    return "\n".join(lines)


def to_consensus_markdown(results: dict[str, Any]) -> str:
    """Consensus summary and per-profile inclusion details."""
    lines = [
        "# Consensus across profiles",
        "",
        f"> {DISCLAIMER}",
        "",
        "The letter distribution shows how many profiles landed on each grade — a",
        "contested stock is a different proposition depending on the lens, and that",
        "disagreement is the most useful thing this table reports.",
        "",
        "| ticker | consensus | score | letters | best as | worst as |",
        "|---|:---:|---:|---:|---|---|",
    ]
    ordered = sorted(
        results.values(),
        key=lambda result: (
            not bool(len(result.scores)),
            -result.score if math.isfinite(result.score) else 1e9,
            result.ticker,
        ),
    )
    for result in ordered:
        if not len(result.scores):
            lines.append(f"| {result.ticker} | N/A | — | — | could not be graded | — |")
            continue
        lines.append(
            f"| {result.ticker} | {result.letter} | {result.score:.1f} "
            f"| {_letter_distribution_text(result)} "
            f"| {result.best_profile} ({result.scores.max():.0f}) "
            f"| {result.worst_profile} ({result.scores.min():.0f}) |"
        )

    for result in ordered:
        lines += [
            "",
            f"## {result.ticker} by profile",
            "",
            "| profile | grade | score | consensus status |",
            "|---|:---:|---:|---|",
        ]
        per_profile = sorted(
            result.per_profile.items(),
            key=lambda item: (
                not item[1].graded,
                -item[1].score if math.isfinite(item[1].score) else 1e9,
                item[0],
            ),
        )
        for name, report in per_profile:
            score = f"{report.score:.1f}" if math.isfinite(report.score) else "—"
            status = "included" if report.graded else "excluded (not graded)"
            lines.append(f"| {name} | {report.letter} | {score} | {status} |")
    return "\n".join(lines)
