"""Rendering: terminal, JSON, and Markdown.

The terminal report is the primary interface and is built around one principle — **a grade that
cannot be argued with is not useful**. So every report shows the confidence interval beside the
score, the data coverage behind it, which attributes pushed the grade up and down, and any warning
that qualifies it (synthetic prices, missing pillars, a weighting method that had to fall back).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date
from typing import Any

from rich.box import SIMPLE
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .types import GradeReport

__all__ = ["render_consensus", "render_ranking", "render_report", "to_json", "to_markdown"]

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


def render_report(report: GradeReport, console: Console | None = None, *, explain: bool = False) -> None:
    """Render one security's grade to the terminal."""
    console = console or Console()

    header = Text()
    header.append(f"{report.ticker}  ", style="bold white")
    header.append(f"{report.letter}", style=f"bold {_colour(report.letter)}")
    header.append(f"  {report.score:.1f}/100", style="white")
    if report.ci and not math.isnan(report.ci[0]):
        header.append(f"   90% CI [{report.ci[0]:.0f}–{report.ci[1]:.0f}]", style="dim")
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
                b[0], f"{b[1]:+.2f}" if b[0] else "",
                w[0], f"{w[1]:+.2f}" if w[0] else "",
            )
        blocks.append(drivers)

    if report.warnings:
        warn = Text()
        for message in report.warnings[:8]:
            warn.append(f"! {message}\n", style="yellow")
        blocks.append(warn)

    console.print(Panel(Group(*blocks), border_style=_colour(report.letter), title="stock-grader",
                        title_align="left"))


def render_ranking(reports: dict[str, GradeReport], console: Console | None = None, *, top: int | None = None) -> None:
    """Render a ranked universe."""
    console = console or Console()
    table = Table(box=SIMPLE, header_style="bold", title="Ranked universe", title_justify="left")
    table.add_column("#", justify="right", width=4, style="dim")
    table.add_column("ticker", style="bold")
    table.add_column("grade", justify="center")
    table.add_column("score", justify="right")
    table.add_column("", width=22)
    table.add_column("90% CI", justify="center", style="dim")
    table.add_column("cov", justify="right", style="dim")
    table.add_column("sector", style="dim")

    # Ungraded names sort last regardless of score: an N/A at the top of a ranked list reads as
    # "best", when it means the opposite — we declined to judge it.
    ordered = sorted(
        reports.values(),
        key=lambda r: (r.letter == "N/A", -r.score if not math.isnan(r.score) else 1e9),
    )
    if top:
        ordered = ordered[:top]
    for i, report in enumerate(ordered, 1):
        ci = f"{report.ci[0]:.0f}–{report.ci[1]:.0f}" if report.ci and not math.isnan(report.ci[0]) else "—"
        table.add_row(
            str(i),
            report.ticker,
            Text(report.letter, style=f"bold {_colour(report.letter)}"),
            f"{report.score:.1f}",
            _bar(report.score, 20),
            ci,
            f"{report.coverage:.0%}",
            str(report.meta.get("sector", "")),
        )
    console.print(table)


def render_consensus(results: dict, console: Console | None = None) -> None:
    """Render the multi-profile consensus, foregrounding disagreement."""
    console = console or Console()
    table = Table(box=SIMPLE, header_style="bold",
                  title="Consensus across profiles — low clarity means the answer depends on the lens",
                  title_justify="left")
    table.add_column("ticker", style="bold")
    table.add_column("consensus", justify="center")
    table.add_column("score", justify="right")
    table.add_column("clarity", justify="right")
    table.add_column("best as", style="green")
    table.add_column("worst as", style="red")

    for result in sorted(results.values(), key=lambda r: -r.score if not math.isnan(r.score) else 1e9):
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
        clarity_style = "green" if result.clarity > 60 else "yellow" if result.clarity > 30 else "red"
        table.add_row(
            result.ticker,
            Text(result.letter, style=f"bold {_colour(result.letter)}"),
            f"{result.score:.1f}",
            Text(f"{result.clarity:.0f}", style=clarity_style),
            f"{result.best_profile} ({result.scores.max():.0f})",
            f"{result.worst_profile} ({result.scores.min():.0f})",
        )
    console.print(table)


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _encode(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def to_json(reports: GradeReport | dict[str, GradeReport], *, indent: int = 2) -> str:
    """Stable JSON representation, suitable for piping into other tools."""
    if isinstance(reports, GradeReport):
        return json.dumps(_encode(reports), indent=indent)
    return json.dumps({k: _encode(v) for k, v in reports.items()}, indent=indent)


def to_markdown(report: GradeReport) -> str:
    """Markdown report for pasting into notes or a PR."""
    lines = [
        f"# {report.ticker} — {report.letter} ({report.score:.1f}/100)",
        "",
        f"- **Profile**: {report.profile}",
        (f"- **Weighting**: {report.weighting_method}   **Normalizer**: {report.normalizer}   "
         + f"**Aggregator**: {report.aggregator}"),
        (f"- **Coverage**: {report.coverage:.0%} "
         + f"({report.explain.get('n_metrics_ok', 0)} computed, "
         + f"{report.explain.get('n_metrics_missing', 0)} missing, "
         + f"{report.explain.get('n_metrics_not_applicable', 0)} not applicable)"),
    ]
    if report.ci and not math.isnan(report.ci[0]):
        lines.append(f"- **90% confidence interval**: {report.ci[0]:.1f} – {report.ci[1]:.1f}")
    if report.percentile is not None:
        lines.append(f"- **Universe percentile**: {report.percentile:.0f}")
    lines += ["", "## Pillars", "", "| pillar | score | weight | contribution |", "|---|---:|---:|---:|"]
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
