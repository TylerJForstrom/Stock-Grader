# Contributing

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export STOCK_GRADER_CONTACT="you@example.com"   # SEC asks for a contact in the User-Agent
.venv/bin/pytest -q
```

The test suite is **fully offline** — every test builds its own fixtures, and the whole suite passes
with the network blocked at the socket layer. If a test you add needs the network, it does not
belong in the suite; put it in `scripts/` instead.

## Adding a metric

Drop a decorated function into `src/stock_grader/metrics/`. Nothing else needs to change — the
registry discovers it and the CLI, reports and every weighting method pick it up.

```python
@metric("my_metric", pillar="quality", direction=1, unit="ratio", winsor=(0.0, 5.0))
def my_metric(s: SecuritySnapshot) -> float | None:
    """One line saying what it measures and why it belongs in that pillar."""
    return safe_div(_ttm(s, "numerator"), _latest(s, "denominator"), positive_denominator=True)
```

Rules that are not negotiable, because breaking them produces a *confidently wrong grade* rather
than an error:

1. **Return `None`, never a zero or a sentinel.** A zero is an assertion that the company scored
   badly. `None` becomes `MISSING`, the weight renormalises away, and the report says so.
2. **Use the guarded helpers in `metrics/util.py`.** `safe_div(..., positive_denominator=True)` for
   every valuation multiple — a loss-making company has no P/E, not a negative one, and an
   unguarded negative sorts to the top of "cheapest first".
3. **Declare `direction` honestly.** `0` means non-monotonic, and then `ideal_band` is required.
   A payout ratio of 95% is not the best dividend stock in the universe.
4. **Declare what you need**: `needs_prices`, `needs_benchmark`, `needs_risk_free`, `min_history`.
   The engine enforces them before your function runs.
5. **Sector-specific metrics** must be added to `BANK_ONLY_METRICS` or `REIT_ONLY_METRICS` in
   `data/sectors.py`, so companies they do not apply to are marked `NOT_APPLICABLE` rather than
   charged a coverage penalty.

## Adding a weighting method

Drop a decorated function into `weighting.py`. It must satisfy the shared contract: weights
non-negative, finite, summing to 1, indexed by the columns of `X`, and it must declare
`needs_panel` / `needs_returns` plus a `fallback` for when those preconditions fail. A single-stock
grade gives a one-row panel, and every data-driven method has to degrade gracefully there.

`tests/test_invariants.py` enforces all of this for every registered method automatically — you get
the tests for free, and they will fail if the contract is broken.

## What a good change looks like

This codebase has a house style about correctness, learned the hard way. Every serious bug found in
it produced a *confident wrong number*, not a crash:

- Lowe's read as 92% debt-free (a tag abandoned in 2009 still resolved)
- Toyota's revenue came out 150x too large (JPY read as USD)
- a true random walk scored Hurst 0.5996 (uncorrected estimator bias)
- Bed Bath & Beyond scored Altman "safe" ten months before Chapter 11

So: **prefer refusing to answer over answering wrongly.** When you fix something, add a regression
test that fails on the old code, and put the measured evidence in the docstring — the number, the
company, the date. A comment saying *why* a guard exists is worth more than the guard.

## Before opening a PR

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/pytest -q
```
