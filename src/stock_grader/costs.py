"""Per-name, per-date trading-cost model — the load-bearing half of a small-cap claim.

An "edge" that survives a flat 5 bps and dies at a realistic cost is not an
edge. The evaluator's historical charge was a single number applied to every
name in every band: it undercharges thinly traded names and overcharges the
largest ones, which is exactly the bias that fabricates a small-cap edge out of
nothing. This module replaces that constant with a per-row estimate built from
two published estimators and one archive-measured floor.

**Composition** (all quantities in basis points of price):

.. code-block:: text

    spread_bps    = max( CS21 , 10^4 * TICK_SIZE_USD / price )     full spread
    half_bps      = spread_bps / 2
    u             = notional_allowed / adv20_dollar                participation
    g             = ATHL_GAMMA * sigma * u ** ATHL_ALPHA           permanent
    h             = ATHL_ETA   * sigma * u ** ATHL_BETA            temporary
    athl_bps      = 10^4 * (g / 2 + h)
    amihud_bps    = amihud_lambda_bps_per_musd * notional_allowed / 10^6
    impact_bps    = max(athl_bps, amihud_bps)
    one_way_bps   = half_bps + impact_bps
    round_trip    = 2 * one_way_bps

**Sources.** The spread term is Corwin & Schultz (2012), high-low, with their
overnight-gap adjustment, over a 21-session window. The impact term is Almgren,
Thum, Hauptmann & Li (2005), "Direct Estimation of Equity Market Impact", at
their published calibration, for an order worked over one full session
(``T = 1``), so ``u`` is both the ADV fraction and the participation rate. The
floor is Amihud (2002), measured on the same window from the same bars.

**Three rules that are not negotiable, and why.**

1. *Corwin-Schultz negative estimates are floored ONCE, on the window mean.*
   Roughly two of every five two-day estimates come out negative. Flooring each
   PAIR at zero before averaging is a truncation bias that scales with
   volatility rather than with spread; on a real archive it inflates the most
   liquid names' median spread by more than an order of magnitude and inverts
   the liquidity gradient. :func:`corwin_schultz_spread_bps` therefore averages
   the raw pair estimates and floors the mean.

2. *Taking the larger of ATHL and Amihud is a conservatism rule declared in
   advance, not a fitted blend.* The ATHL calibration comes from a large/mid-cap
   institutional sample and may understate the thin tail. Overstating cost
   cannot manufacture an edge; understating it can. The asymmetry is the point.

3. *A window without enough usable pairs yields NO estimate.* Not a band
   average, not a back-fill, not a fabricated constant —
   :func:`estimate_cost` returns ``None`` and the caller counts the refusal.

**What this model does not capture** — every one of these is a real cost that a
result built on it silently omits: no quote data or NBBO (the spread is never
observed, and a range-based stand-in is provably blind to liquidity in exactly
the thin tail this exists to price); no intraday data at all, so no timing risk,
VWAP slippage, auction dynamics or imbalance; no borrow cost and no locate, so
any long-short statistic is reported unfinanced; no commissions, SEC Section 31
fee or FINRA TAF; no halts or limit bands; no adverse selection conditional on
the signal; no cross-name or portfolio-level impact; symmetric entry and exit by
assumption, which daily bars cannot test; and bid-ask bounce in the close
prints, which inflates a return's variance more in thin names than in liquid
ones with no correction available.

**The old flat model is a degenerate case of this one**, and that is pinned by
test rather than asserted: with ``CS = 0``, a price whose tick floor is
``2 * FLAT_ONE_WAY_BPS``, ``sigma = 0`` and ``lambda = 0``, the composition
above returns a 5 bps one-way and a 10 bps round trip — the historical charge,
exactly. See ``GOLDEN_VECTORS[0]``.

**Cross-repo agreement.** Stock-Vault carries an independent copy of this model
(the no-cross-repo-imports rule forbids sharing the code). The two are pinned to
each other by ``config/cost_golden_vectors.json``, whose bytes and sha256 both
repositories assert. A silent drift between the simulator's cost and the
evaluator's cost would make every net number in the program uninterpretable, so
the agreement is a test, not a convention.

**Licensing.** Every number in this module and in the golden-vector file is
either a published academic constant or a synthetic input chosen for arithmetic
convenience. No archive measurement — no band median, no measured spread, no
per-ticker value — appears here. Stock-Grader is public and stays that way.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

__all__ = [
    "ADV_PARTICIPATION_CAP",
    "ATHL_ALPHA",
    "ATHL_BETA",
    "ATHL_ETA",
    "ATHL_GAMMA",
    "COST_MODEL_ID",
    "CS_WINDOW_SESSIONS",
    "FLAT_ONE_WAY_BPS",
    "GOLDEN_VECTORS",
    "MIN_USABLE_PAIRS",
    "TICK_SIZE_USD",
    "CostEstimate",
    "CostInputs",
    "amihud_impact_bps",
    "amihud_lambda_bps_per_musd",
    "athl_impact_bps",
    "close_to_close_sigma",
    "corwin_schultz_spread_bps",
    "cost_inputs_from_bars",
    "estimate_cost",
    "golden_vector_sha256",
    "spread_bps",
    "tick_floor_bps",
]

#: Names the model, not the module. It is stamped into every panel that carries
#: a cost column so a later reader can tell which cost priced which row, and it
#: is the grader-side counterpart of the simulator's ``SIM_VERSION``.
COST_MODEL_ID = "cost-v1-cs2012-athl2005-amihud2002"

#: Estimation window, in archived sessions, shared by the spread, the volatility
#: and the Amihud coefficient. One window, so the three terms of a row's cost
#: describe the same stretch of tape.
CS_WINDOW_SESSIONS = 21

#: A window holding fewer usable consecutive-session pairs than this yields no
#: estimate at all. Below roughly half a window the estimators are reading
#: stale prints rather than markets, and a stale print reads as "no range",
#: which a range-based estimator reports as "no spread" — an optimistic
#: fabrication in precisely the names that can least afford one.
MIN_USABLE_PAIRS = 11

#: US minimum tick above $1.00. You cannot cross a spread narrower than one
#: tick, so this is a hard institutional bound rather than a fitted parameter.
TICK_SIZE_USD = 0.01

# Almgren, Thum, Hauptmann & Li (2005), published calibration. These are the
# paper's numbers and are never fitted here.
ATHL_GAMMA = 0.314
ATHL_ALPHA = 0.891
ATHL_ETA = 0.142
ATHL_BETA = 0.600

#: No simulated order may exceed this fraction of the name's 20-session dollar
#: ADV. Conservative against the 10-20% participation convention, deliberately:
#: a capacity claim measured at an aggressive participation rate is a claim
#: about the model rather than about the market. Truncation is counted and
#: reported, never applied silently.
ADV_PARTICIPATION_CAP = 0.01

#: The historical flat charge, one way. Retained as the constant the degenerate
#: case pins, not as a live default: nothing in this module reads it.
FLAT_ONE_WAY_BPS = 5.0

_GOLDEN_PATH = Path(__file__).resolve().parent / "config" / "cost_golden_vectors.json"


@dataclass(frozen=True, slots=True)
class CostInputs:
    """The liquidity state of one name on one date.

    ``cs_spread_bps`` is the raw Corwin-Schultz window estimate as a FULL
    spread in basis points of price, already floored at zero on the window mean
    and NOT yet floored at the tick — :func:`spread_bps` applies that, so a
    caller cannot accidentally apply it twice or forget it once.
    """

    price: float
    adv20_dollar: float
    sigma: float
    cs_spread_bps: float
    amihud_lambda_bps_per_musd: float
    usable_pairs: int

    def is_estimable(self) -> bool:
        """Whether an honest estimate can be produced from these inputs."""

        finite = all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in (
                self.price,
                self.adv20_dollar,
                self.sigma,
                self.cs_spread_bps,
                self.amihud_lambda_bps_per_musd,
            )
        )
        if not finite:
            return False
        if self.usable_pairs < MIN_USABLE_PAIRS:
            return False
        if self.price <= 0.0 or self.adv20_dollar <= 0.0:
            return False
        return (
            self.sigma >= 0.0
            and self.cs_spread_bps >= 0.0
            and (self.amihud_lambda_bps_per_musd >= 0.0)
        )


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Every term of one row's cost, kept separately so none of them hides.

    A single ``round_trip_bps`` cannot be audited after the fact: it cannot say
    whether the charge came from the spread, from the model impact or from the
    measured floor, and it cannot say whether the position was truncated to fit
    the name. All three questions are asked of every band in this program, so
    all three answers are carried on the row.
    """

    spread_bps: float
    half_spread_bps: float
    tick_floor_bps: float
    athl_impact_bps: float
    amihud_impact_bps: float
    impact_bps: float
    one_way_bps: float
    round_trip_bps: float
    participation: float
    notional_target_usd: float
    notional_allowed_usd: float
    capacity_truncated: bool
    truncated_notional_fraction: float

    def to_dict(self) -> dict:
        return asdict(self)


# -- the individual terms ------------------------------------------------------


def tick_floor_bps(price: float) -> float:
    """One tick as a fraction of price, in basis points, as a FULL spread."""

    if not math.isfinite(price) or price <= 0.0:
        raise ValueError("price must be positive and finite to floor a spread at the tick")
    return 1e4 * TICK_SIZE_USD / float(price)


def spread_bps(cs_spread_bps: float, price: float) -> float:
    """The chosen spread term: Corwin-Schultz, floored at the one-cent tick."""

    if not math.isfinite(cs_spread_bps) or cs_spread_bps < 0.0:
        raise ValueError("cs_spread_bps must be a finite, non-negative full spread in bps")
    return max(float(cs_spread_bps), tick_floor_bps(price))


def athl_impact_bps(sigma: float, participation: float) -> float:
    """Almgren-Thum-Hauptmann-Li one-way impact for a full-session order.

    ``g / 2`` is the paper's own accounting for a complete execution at a
    constant rate: the trade pays half of the permanent impact plus all of the
    temporary impact.
    """

    if not math.isfinite(sigma) or sigma < 0.0:
        raise ValueError("sigma must be finite and non-negative")
    if not math.isfinite(participation) or participation < 0.0:
        raise ValueError("participation must be finite and non-negative")
    if participation == 0.0 or sigma == 0.0:
        return 0.0
    permanent = ATHL_GAMMA * sigma * participation**ATHL_ALPHA
    temporary = ATHL_ETA * sigma * participation**ATHL_BETA
    return 1e4 * (permanent / 2.0 + temporary)


def amihud_impact_bps(amihud_lambda_bps_per_musd: float, notional_usd: float) -> float:
    """Amihud (2002) illiquidity applied to a notional, in bps of price.

    ``lambda`` is carried in its reporting unit — basis points of price moved
    per $1M traded — because that is the unit a human can sanity-check, and a
    raw return-per-dollar coefficient is a number nobody can eyeball.
    """

    if not math.isfinite(amihud_lambda_bps_per_musd) or amihud_lambda_bps_per_musd < 0.0:
        raise ValueError("amihud lambda must be finite and non-negative")
    if not math.isfinite(notional_usd) or notional_usd < 0.0:
        raise ValueError("notional must be finite and non-negative")
    return float(amihud_lambda_bps_per_musd) * float(notional_usd) / 1e6


# -- the estimators, from raw bars --------------------------------------------


def _usable_pairs(
    values: Sequence[Sequence[float]],
    excluded: Sequence[bool] | None,
) -> list[int]:
    """Indices ``t`` whose pair ``(t, t + 1)`` is usable by every estimator.

    A pair is unusable when either session carries a non-positive or non-finite
    price, or when the caller has excluded it — the split exclusion, which the
    caller owns because only it can confirm an ex-date against a corporate
    action table. A split reads as a large close-to-close return and as an
    overnight gap the Corwin-Schultz adjustment will happily "correct" by
    shifting a whole session's range, so an unexcluded split does not degrade
    the estimate, it corrupts it.
    """

    series = [list(map(float, column)) for column in values]
    length = min((len(column) for column in series), default=0)
    if excluded is not None and len(excluded) < max(length - 1, 0):
        raise ValueError("excluded must carry one flag per consecutive-session pair")
    usable: list[int] = []
    for index in range(length - 1):
        if excluded is not None and bool(excluded[index]):
            continue
        window = [column[index] for column in series] + [column[index + 1] for column in series]
        if all(math.isfinite(value) and value > 0.0 for value in window):
            usable.append(index)
    return usable


def corwin_schultz_spread_bps(
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    excluded_pairs: Sequence[bool] | None = None,
    min_pairs: int = MIN_USABLE_PAIRS,
) -> tuple[float | None, int]:
    """Corwin & Schultz (2012) high-low spread over a window, in bps, plus the
    number of usable pairs.

    Returns ``(None, n)`` when ``n < min_pairs``: an honest refusal, never a
    thinly-supported number that reads the same as a well-supported one.

    The window mean of the RAW pair estimates is floored at zero exactly once.
    See rule 1 in the module docstring for what per-pair flooring does to a
    real cross-section.
    """

    pairs = _usable_pairs([highs, lows], excluded_pairs)
    if len(pairs) < min_pairs:
        return None, len(pairs)
    high = [float(value) for value in highs]
    low = [float(value) for value in lows]
    denominator = 3.0 - 2.0 * math.sqrt(2.0)
    estimates: list[float] = []
    for index in pairs:
        first = math.log(high[index] / low[index]) ** 2
        second = math.log(high[index + 1] / low[index + 1]) ** 2
        beta = first + second
        span = math.log(max(high[index], high[index + 1]) / min(low[index], low[index + 1]))
        gamma = span**2
        alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / denominator - math.sqrt(
            gamma / denominator
        )
        estimates.append(2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha)))
    mean = sum(estimates) / len(estimates)
    return 1e4 * max(mean, 0.0), len(pairs)


def close_to_close_sigma(
    closes: Sequence[float],
    *,
    excluded_pairs: Sequence[bool] | None = None,
    min_pairs: int = MIN_USABLE_PAIRS,
) -> tuple[float | None, int]:
    """Sample standard deviation of daily log close-to-close returns.

    Excluded pairs are dropped rather than zeroed: a split left in the window
    reads as a -50% session and would dominate the volatility of an otherwise
    ordinary name.
    """

    pairs = _usable_pairs([closes], excluded_pairs)
    if len(pairs) < min_pairs:
        return None, len(pairs)
    close = [float(value) for value in closes]
    returns = [math.log(close[index + 1] / close[index]) for index in pairs]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance), len(pairs)


def amihud_lambda_bps_per_musd(
    closes: Sequence[float],
    volumes: Sequence[float],
    *,
    excluded_pairs: Sequence[bool] | None = None,
    min_pairs: int = MIN_USABLE_PAIRS,
) -> tuple[float | None, int]:
    """Amihud (2002) illiquidity: MEDIAN daily ``|r| / $volume`` over the window,
    reported as basis points of price moved per $1M traded.

    Median, not mean, for the same reason the ADV screen uses one: a single
    near-zero-volume session sends the mean to infinity and would price the name
    as untradable on the strength of one bad print.
    """

    pairs = _usable_pairs([closes], excluded_pairs)
    close = [float(value) for value in closes]
    volume = [float(value) for value in volumes]
    if len(volume) < len(close):
        raise ValueError("volumes must cover every session in closes")
    ratios: list[float] = []
    for index in pairs:
        dollar_volume = close[index + 1] * volume[index + 1]
        if not math.isfinite(dollar_volume) or dollar_volume <= 0.0:
            continue
        move = abs(math.log(close[index + 1] / close[index]))
        ratios.append(move / dollar_volume)
    if len(ratios) < min_pairs:
        return None, len(ratios)
    ratios.sort()
    middle = len(ratios) // 2
    median = ratios[middle] if len(ratios) % 2 else (ratios[middle - 1] + ratios[middle]) / 2.0
    return 1e4 * median * 1e6, len(ratios)


def cost_inputs_from_bars(
    *,
    price: float,
    adv20_dollar: float,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    excluded_pairs: Sequence[bool] | None = None,
    min_pairs: int = MIN_USABLE_PAIRS,
) -> CostInputs | None:
    """Build :class:`CostInputs` from one name's trailing window of raw bars.

    Returns ``None`` when any term refuses. The three estimators must agree on
    the same window: a row costed from a spread over 21 sessions and a sigma
    over 4 is not a measurement of anything.
    """

    spread, spread_pairs = corwin_schultz_spread_bps(
        highs, lows, excluded_pairs=excluded_pairs, min_pairs=min_pairs
    )
    sigma, sigma_pairs = close_to_close_sigma(
        closes, excluded_pairs=excluded_pairs, min_pairs=min_pairs
    )
    lam, lambda_pairs = amihud_lambda_bps_per_musd(
        closes, volumes, excluded_pairs=excluded_pairs, min_pairs=min_pairs
    )
    if spread is None or sigma is None or lam is None:
        return None
    inputs = CostInputs(
        price=float(price),
        adv20_dollar=float(adv20_dollar),
        sigma=sigma,
        cs_spread_bps=spread,
        amihud_lambda_bps_per_musd=lam,
        usable_pairs=min(spread_pairs, sigma_pairs, lambda_pairs),
    )
    return inputs if inputs.is_estimable() else None


# -- composition ---------------------------------------------------------------


def estimate_cost(
    inputs: CostInputs,
    notional_usd: float,
    *,
    participation_cap: float = ADV_PARTICIPATION_CAP,
) -> CostEstimate | None:
    """Round-trip cost for one name, one date, one position size.

    ``None`` means the inputs cannot support an honest estimate (§ the
    ``MIN_USABLE_PAIRS`` refusal). The caller counts that refusal and drops the
    row; it does not substitute an average.
    """

    if not inputs.is_estimable():
        return None
    if not math.isfinite(notional_usd) or notional_usd < 0.0:
        raise ValueError("notional_usd must be finite and non-negative")
    if participation_cap <= 0.0 or (math.isnan(participation_cap)):
        raise ValueError("participation_cap must be positive")

    target = float(notional_usd)
    ceiling = math.inf if math.isinf(participation_cap) else participation_cap * inputs.adv20_dollar
    allowed = min(target, ceiling)
    truncated_fraction = 0.0 if target <= 0.0 else max(0.0, 1.0 - allowed / target)

    full_spread = spread_bps(inputs.cs_spread_bps, inputs.price)
    half = full_spread / 2.0
    participation = allowed / inputs.adv20_dollar
    athl = athl_impact_bps(inputs.sigma, participation)
    amihud = amihud_impact_bps(inputs.amihud_lambda_bps_per_musd, allowed)
    impact = max(athl, amihud)
    one_way = half + impact
    return CostEstimate(
        spread_bps=full_spread,
        half_spread_bps=half,
        tick_floor_bps=tick_floor_bps(inputs.price),
        athl_impact_bps=athl,
        amihud_impact_bps=amihud,
        impact_bps=impact,
        one_way_bps=one_way,
        round_trip_bps=2.0 * one_way,
        participation=participation,
        notional_target_usd=target,
        notional_allowed_usd=allowed,
        capacity_truncated=allowed < target,
        truncated_notional_fraction=truncated_fraction,
    )


# -- the cross-repo pin --------------------------------------------------------


@lru_cache(maxsize=1)
def _golden_payload() -> dict:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def golden_vector_sha256() -> str:
    """sha256 of the golden-vector file's CANONICAL CONTENT.

    Stock-Vault carries the same file. One hash comparison decides whether the
    two independent implementations are still pinned to the same arithmetic;
    without it a divergence would surface months later as an unexplained gap
    between simulated and evaluated cost, at which point neither number could
    be trusted.

    The hash is taken over ``json.dumps(payload, sort_keys=True,
    separators=(",", ":"))`` rather than over the file's raw bytes, and that is
    the load-bearing detail. A raw-byte hash of a *text* file is not a portable
    pin: git rewrites line endings on checkout (a Windows runner with
    ``core.autocrlf`` turns every ``\\n`` into ``\\r\\n``), so the same commit
    hashes differently on two machines and the pin fails for a reason that has
    nothing to do with the cost model. A cross-repository agreement that breaks
    on a checkout setting is not an agreement. Canonicalising first pins what
    the file *says*, which is the thing the two repositories actually have to
    agree about — and it lets either side reformat its copy without a
    false alarm. ``.gitattributes`` separately keeps the stored bytes at LF, so
    the two copies stay byte-identical as well; that is a convenience, this is
    the guarantee.
    """

    canonical = json.dumps(
        _golden_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _load_golden() -> tuple[dict, ...]:
    return tuple(_golden_payload()["vectors"])


#: Synthetic worked examples every implementation of this model must reproduce.
#: Inputs are round numbers chosen for arithmetic convenience and carry no
#: archive content whatsoever (see the licensing note in the module docstring).
GOLDEN_VECTORS: tuple[dict, ...] = _load_golden()
