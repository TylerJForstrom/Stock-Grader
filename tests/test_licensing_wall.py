"""The licensing wall, enforced on this PUBLIC repository's own documentation.

Stock-Grader is public (``docs/majors/ORIENTATION.md`` records why it must stay
that way: paper-trader.yml checks it out with the default ``GITHUB_TOKEN``,
which cannot read a private repo). Stock-Vault's archives are not: FINRA is
"non-commercial internal use only; redistribution prohibited", Finnhub forbids
redistribution of data **or derived results**, IB permits "derived aggregates
only", SSGA is "private research archive only", and Massive/Polygon's terms were
never machine-verified, which under PIT discipline resolves conservatively.

The wall was breached by prose, not by code: design documents quoted the values
they had measured — a named ticker's borrow fee, a vendor's exact file
composition, a fractional EOD volume for a named ticker on a named date, and a
complete measured panel result (row counts, a cross-sectional distribution, and
a realized forward-return range). Every one of those is recoverable from public
git history forever.

This module is the standing gate, modelled on Stock-Data's ``test_workflows``
text checks. It is deliberately narrow: it does not ban numbers, and it does not
ban naming the sources. It bans the shapes that only a measurement produces —
a per-ticker vendor value, a thousands-separated row count, a dollar-per-day
figure — on a line that also names a licensed source or a vault data path.

Adding a genuinely public number that trips this gate is possible; the fix is to
put the number in Stock-Vault, not to widen the pattern.

Scope, and what the gate still cannot see. It was ``docs/**/*.md`` only, which
is how a *test docstring* published a measured capacity fraction from the
private archive to public main. ``src/`` and ``tests/`` are now scanned on the
same terms: a comment is published exactly as loudly as a document. What
remains invisible is a bare percentage — "roughly 79% unfilled" matches no shape
here, because ``1% of ADV20$`` and ``a 99% coverage bar`` are specification
parameters that look identical to a regex. That gap is deliberate and is the
reason this gate is a floor and not a substitute for review: it catches the
shapes only a vendor file produces, and it does not catch a measurement stated
in prose. Widening the shapes to cover percentages would fire on the spec far
more often than on a leak, and a gate that cries wolf gets suppressed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"

#: Naming a licensed source is fine. Naming one ON THE SAME LINE as a measured
#: value is what publishes the measurement.
LICENSED_CONTEXT = re.compile(
    r"(?i)\b("
    r"finra|finnhub|ssga|interactive\s+brokers|\bIB\b|borrow|"
    r"massive|polygon|market_eod|short[_ ]interest|rec_trends|ssga_holdings|"
    r"delisted|stockanalysis|Stock-Vault/data|"
    # Vault archive filenames, which name their source without naming it:
    # shrt<YYYYMMDD>.csv (FINRA), usa_<stamp>.jsonl.gz (IB), finnhub_<month>.
    r"shrt\d{8}|usa_\d{8}T|finnhub_\d{4}"
    r")\b"
)

#: Value shapes a measurement produces and a specification does not.
MEASURED_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 12,482 / 19,719 / 5,890 — a thousands-separated count read off an archive.
    ("thousands-separated count", re.compile(r"\b\d{1,3}(?:,\d{3})+\b")),
    # $315.1M/day, $46B/day — a dollar-volume figure from the vendor's prices.
    # A bare round threshold ("close*volume >= $1M") is a SPEC parameter, not a
    # measurement, so the pattern needs a decimal or an explicit per-day rate.
    ("dollar-magnitude figure", re.compile(r"\$\s?(?:\d+\.\d+\s*[MBK]\b|\d+\s*[MBK]/day)")),
    # 51859042.576473 / 0.2782 / 1076.6441 / 739.9603 — a raw vendor value.
    # Four places, not three: repo-computed simulation numbers are quoted to
    # three (E[max IC | null] = 0.306) and are not vendor data.
    ("high-precision vendor value", re.compile(r"\b\d+\.\d{4,}\b")),
)

#: Sentinels that are vendor CONVENTIONS (documented floors/caps/flags), not
#: measurements of the archive, plus this file's own explanatory text.
ALLOWED_SUBSTRINGS = (
    "999.99",  # FINRA's documented daysToCoverQuantity cap
)


def _doc_files() -> list[Path]:
    # docs/forward/ holds committed evaluation artifacts whose numbers are
    # grader-computed from SEC public-domain scores, not vendor data.
    return sorted(
        path
        for path in DOCS.rglob("*.md")
        if "forward" not in path.relative_to(DOCS).parts
        and "calibration" not in path.relative_to(DOCS).parts
    )


def _source_files() -> list[Path]:
    """``src/`` and ``tests/`` — the trees the gate could not see.

    The wall was breached a second time, and this time not by prose: a test
    docstring in this very directory restated a band's measured capacity
    truncation from the private archive. The gate never looked, because it
    globbed ``docs/**/*.md`` and nothing else. Comments and docstrings are
    published exactly as loudly as documentation is, so they are scanned on the
    same terms and by the same patterns.

    This file is excluded from its own scan: it quotes the breaches it exists
    to catch, as fixtures.
    """
    return sorted(
        path
        for tree in ("src", "tests")
        for path in (REPO_ROOT / tree).rglob("*.py")
        if path.resolve() != Path(__file__).resolve()
    )


def _scan(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not LICENSED_CONTEXT.search(line):
                continue
            for label, pattern in MEASURED_SHAPES:
                match = pattern.search(line)
                if match is None:
                    continue
                if any(allowed in match.group(0) for allowed in ALLOWED_SUBSTRINGS):
                    continue
                offenders.append(
                    f"{_label(path)}:{lineno}: {label} "
                    f"{match.group(0)!r} on a line naming a licensed source"
                )
    return offenders


def _label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:  # a planted fixture outside the tree
        return str(path)


def _assert_clean(offenders: list[str]) -> None:
    assert not offenders, (
        "vault-derived measured values must not be published in this PUBLIC repo "
        "(move them into Stock-Vault's private notes):\n  " + "\n  ".join(offenders)
    )


def test_public_docs_carry_no_licensed_measured_values() -> None:
    _assert_clean(_scan(_doc_files()))


def test_source_and_test_comments_carry_no_licensed_measured_values() -> None:
    """The second breach: a docstring is publication too.

    ``tests/test_backtest.py`` restated a band's measured capacity truncation
    from the private archive in a docstring, and shipped it to public main. The
    docs-only gate above could not have caught it — it never opened the file.
    """
    _assert_clean(_scan(_source_files()))


def test_the_scanner_reads_a_python_docstring_the_same_way_it_reads_a_document(
    tmp_path: Path,
) -> None:
    """Scope, proven rather than assumed.

    ``_source_files`` is the whole fix for the second breach, so it needs a
    fixture that fails if the globs are narrowed back to markdown or if a
    Python file is opened as anything other than text.
    """
    planted = tmp_path / "module_with_a_leak.py"
    planted.write_text(
        '"""A docstring, which is published exactly as loudly as a document.\n\n'
        "On the FINRA cross-section the panel carried 12,482 rows.\n"
        '"""\n',
        encoding="utf-8",
    )
    offenders = _scan([planted])
    assert len(offenders) == 1
    assert "thousands-separated count" in offenders[0]
    with pytest.raises(AssertionError, match="PUBLIC repo"):
        _assert_clean(offenders)


def test_the_gate_would_catch_the_breach_it_was_written_for() -> None:
    """The exact prose that was live on public main, as a fixture."""
    breaches = [
        "IB borrow measured on usa_20260729T2317.jsonl.gz: 19,719 rows; AAPL 0.2782",
        "shrt20260715.csv has 22,375 data rows; marketClassCode counts OTC 9491",
        "market_eod volume is a FLOAT (AAPL 2026-07-28 volume 51859042.576473)",
        "MU at $46B/day median vs AAPL $15B on the market_eod ranking",
        "a FINRA DTC panel yields 5,890 rows (3,657 with a resolvable CIK)",
    ]
    for line in breaches:
        assert LICENSED_CONTEXT.search(line), line
        assert any(pattern.search(line) for _, pattern in MEASURED_SHAPES), line


@pytest.mark.parametrize(
    "line",
    [
        "market_eod rows have keys: symbol, open, high, low, close, volume, vwap",
        "FINRA's daysToCoverQuantity is FLOORED at 1.00 and CAPPED at 999.99",
        "the free tier is a rolling ~730-day archive, so early sessions roll off",
        "1,200 profiles is a made-up number on a line naming nothing licensed",
    ],
)
def test_the_gate_does_not_fire_on_schema_or_convention_prose(line: str) -> None:
    if not LICENSED_CONTEXT.search(line):
        return
    for _, pattern in MEASURED_SHAPES:
        match = pattern.search(line)
        assert match is None or any(a in match.group(0) for a in ALLOWED_SUBSTRINGS), line
