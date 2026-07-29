"""Performance metrics — pure Python.

Everything here operates on plain ``list[float]``. Callers convert Decimal
prices to floats at the boundary; statistical work doesn't need Decimal
precision and floats keep the math fast and dependency-free.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

TRADING_DAYS = 252


def simple_returns(prices: Sequence[float]) -> list[float]:
    """r_t = p_t / p_{t-1} - 1."""
    return [
        prices[i] / prices[i - 1] - 1.0
        for i in range(1, len(prices))
        if prices[i - 1] != 0
    ]


def log_returns(prices: Sequence[float]) -> list[float]:
    """r_t = ln(p_t / p_{t-1}). Log returns are additive across time."""
    return [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i - 1] > 0 and prices[i] > 0
    ]


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: Sequence[float], *, sample: bool = True) -> float:
    """Standard deviation. Sample (n-1) by default."""
    n = len(xs)
    if n < 2:
        return 0.0
    mu = mean(xs)
    ss = sum((x - mu) ** 2 for x in xs)
    return math.sqrt(ss / (n - 1 if sample else n))


def annualized_volatility(
    returns: Sequence[float], periods_per_year: int = TRADING_DAYS
) -> float:
    """Per-period volatility scaled to annual by sqrt-of-time."""
    return stdev(returns) * math.sqrt(periods_per_year)


def sharpe_ratio(
    returns: Sequence[float],
    *,
    risk_free_per_period: float = 0.0,
    periods_per_year: int = TRADING_DAYS,
) -> float:
    """Annualized Sharpe ratio.

    Sharpe is a *ranking* tool, not a verdict — it ignores skew and fat tails
    (docs/roadmap/MILESTONES.md#m8). Reported here for comparison, not worship.
    """
    if not returns:
        return 0.0
    excess = [r - risk_free_per_period for r in returns]
    sd = stdev(excess)
    if sd == 0:
        return 0.0
    return (mean(excess) / sd) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction.

    Path-dependent — it cannot be recovered from start/end values alone
    (docs/roadmap/MILESTONES.md#m4).
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            worst = max(worst, dd)
    return worst


def autocorrelation(series: Sequence[float], lag: int) -> float:
    """Sample autocorrelation at ``lag``."""
    n = len(series)
    if n <= lag or n < 2:
        return 0.0
    mu = mean(series)
    denom = sum((x - mu) ** 2 for x in series)
    if denom == 0:
        return 0.0
    num = sum((series[i] - mu) * (series[i - lag] - mu) for i in range(lag, n))
    return num / denom


def excess_kurtosis(series: Sequence[float]) -> float:
    """Excess kurtosis (normal = 0). Positive means fatter tails than normal."""
    n = len(series)
    if n < 4:
        return 0.0
    mu = mean(series)
    sd = stdev(series, sample=False)
    if sd == 0:
        return 0.0
    m4 = sum((x - mu) ** 4 for x in series) / n
    return m4 / (sd**4) - 3.0


def win_rate(trade_pnls: Sequence[float]) -> float:
    """Fraction of trades with positive P&L."""
    if not trade_pnls:
        return 0.0
    wins = sum(1 for p in trade_pnls if p > 0)
    return wins / len(trade_pnls)


def profit_factor(trade_pnls: Sequence[float]) -> float:
    """Gross profit / gross loss. >1 is profitable; inf if no losing trades."""
    gross_profit = sum(p for p in trade_pnls if p > 0)
    gross_loss = -sum(p for p in trade_pnls if p < 0)
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


# --- ported from the simulation's supervised harness: tie-aware Spearman IC ---
def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def rank_ic(actual: Sequence[float], pred: Sequence[float]) -> float:
    """Spearman rank correlation between predictions and actuals (the IC)."""
    n = min(len(actual), len(pred))
    if n < 2:
        return 0.0
    ra, rp = _ranks(actual[:n]), _ranks(pred[:n])
    ma, mp = mean(ra), mean(rp)
    cov = sum((ra[i] - ma) * (rp[i] - mp) for i in range(n))
    va = sum((ra[i] - ma) ** 2 for i in range(n))
    vp = sum((rp[i] - mp) ** 2 for i in range(n))
    return cov / (va**0.5 * vp**0.5) if va > 0 and vp > 0 else 0.0
