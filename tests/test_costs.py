"""The per-row cost model, pinned term by term.

Three things this file exists to stop:

1. A cost change that silently reprices the historical flat charge. The
   degenerate vector reproduces the old 10 bps round trip exactly; if that
   breaks, every number ever produced under the flat assumption has moved.
2. A drift between this implementation and Stock-Vault's. The two repositories
   may not import each other, so the golden-vector file is the only thing
   holding them together — its sha256 is asserted here as a literal.
3. A refusal quietly turning into a fabrication. A window too short to measure
   must produce nothing, and "nothing" must not be representable as a number.
"""

from __future__ import annotations

import dataclasses
import json
import math

import pytest

from stock_grader import costs

#: sha256 of the golden-vector file's CANONICAL content, as a literal.
#: Stock-Vault asserts the same constant against its own copy.
#:
#: What that does and does not buy, stated at the strength it actually holds.
#: It catches an edit to this copy that does not update this line. It does NOT
#: catch an edit landed here and never landed in the vault: this repository has
#: no way to see the other file, so each side can update its literal in lockstep
#: with its own copy and stay green while the two files diverge. That is exactly
#: what happened — ``bar_vectors`` was added on this side in #35 and never
#: landed in the vault, and both suites passed for a day with a broken pin.
#: The check that closes it runs in Stock-Vault's CI, which fetches THIS file
#: from public main and refuses to build on any difference. The reverse
#: direction is not checkable and this comment does not pretend it is.
#:
#: Canonical content, not raw bytes: git rewrites line endings on checkout, so
#: a byte hash of a text file makes the pin fail on a Windows runner for a
#: reason that has nothing to do with the cost model. See
#: :func:`stock_grader.costs.golden_vector_sha256`.
GOLDEN_SHA256 = "de8b55651b1943f96541f6ba1fcf41fab366e4dbaf40fb26172c5656b4df5be1"


def test_golden_vector_content_is_pinned():
    assert costs.golden_vector_sha256() == GOLDEN_SHA256


def test_the_pin_survives_a_line_ending_rewrite(tmp_path, monkeypatch):
    """The regression for the pin itself.

    A Windows checkout with core.autocrlf hands this module a CRLF copy of the
    same file. The pin must not care: it agrees about what the file says, not
    about how a platform stored it.
    """
    crlf = tmp_path / "cost_golden_vectors.json"
    crlf.write_bytes(costs._GOLDEN_PATH.read_bytes().replace(b"\n", b"\r\n"))
    assert crlf.read_bytes() != costs._GOLDEN_PATH.read_bytes()

    for cached in (costs._golden_payload, costs.golden_vector_sha256):
        cached.cache_clear()
    monkeypatch.setattr(costs, "_GOLDEN_PATH", crlf)
    try:
        assert costs.golden_vector_sha256() == GOLDEN_SHA256
    finally:
        monkeypatch.undo()
        for cached in (costs._golden_payload, costs.golden_vector_sha256):
            cached.cache_clear()


def test_golden_vector_file_declares_the_constants_this_module_uses():
    """The vault reads the constants from the same file it reads the vectors
    from, so a divergence in ATHL's calibration is caught even by a vector whose
    expected outputs happen not to exercise it."""
    payload = json.loads(costs._GOLDEN_PATH.read_text(encoding="utf-8"))
    declared = payload["constants"]
    assert payload["cost_model_id"] == costs.COST_MODEL_ID
    assert declared["ATHL_GAMMA"] == costs.ATHL_GAMMA
    assert declared["ATHL_ALPHA"] == costs.ATHL_ALPHA
    assert declared["ATHL_ETA"] == costs.ATHL_ETA
    assert declared["ATHL_BETA"] == costs.ATHL_BETA
    assert declared["TICK_SIZE_USD"] == costs.TICK_SIZE_USD
    assert declared["ADV_PARTICIPATION_CAP"] == costs.ADV_PARTICIPATION_CAP
    assert declared["MIN_USABLE_PAIRS"] == costs.MIN_USABLE_PAIRS
    assert declared["CS_WINDOW_SESSIONS"] == costs.CS_WINDOW_SESSIONS
    assert declared["FLAT_ONE_WAY_BPS"] == costs.FLAT_ONE_WAY_BPS


@pytest.mark.parametrize("vector", costs.GOLDEN_VECTORS, ids=lambda v: v["name"])
def test_golden_vector_is_reproduced(vector: dict):
    inputs = costs.CostInputs(**vector["inputs"])
    cap = vector["participation_cap"]
    estimate = costs.estimate_cost(
        inputs,
        vector["notional_usd"],
        participation_cap=math.inf if cap is None else cap,
    )
    if vector["expected"] is None:
        assert estimate is None, f"{vector['name']} must refuse: {vector['why']}"
        return
    assert estimate is not None
    produced = estimate.to_dict()
    assert set(produced) == set(vector["expected"])
    for field, expected in vector["expected"].items():
        actual = produced[field]
        if isinstance(expected, bool):
            assert actual is expected, field
        else:
            assert actual == pytest.approx(expected, rel=1e-9, abs=1e-12), field


def test_the_flat_five_bps_model_is_a_degenerate_case_of_this_one():
    """The historical charge, recovered exactly rather than approximately.

    Zero spread estimate, a price whose one-cent tick is exactly a 10 bps full
    spread, no volatility, no measured illiquidity and no participation cap:
    5 bps one way, 10 bps round trip, and a fill at P*(1 +/- 5e-4).
    """
    inputs = costs.CostInputs(
        price=10.0,
        adv20_dollar=1.0e9,
        sigma=0.0,
        cs_spread_bps=0.0,
        amihud_lambda_bps_per_musd=0.0,
        usable_pairs=costs.CS_WINDOW_SESSIONS,
    )
    estimate = costs.estimate_cost(inputs, 1_000.0, participation_cap=math.inf)
    assert estimate is not None
    assert estimate.one_way_bps == costs.FLAT_ONE_WAY_BPS
    assert estimate.round_trip_bps == 2 * costs.FLAT_ONE_WAY_BPS
    assert estimate.impact_bps == 0.0
    buy = 10.0 * (1 + estimate.one_way_bps / 1e4)
    sell = 10.0 * (1 - estimate.one_way_bps / 1e4)
    assert buy == pytest.approx(10.0 * 1.0005, rel=0, abs=1e-12)
    assert sell == pytest.approx(10.0 * 0.9995, rel=0, abs=1e-12)


def test_tick_floor_is_a_hard_bound_not_a_suggestion():
    assert costs.tick_floor_bps(10.0) == pytest.approx(10.0)
    assert costs.tick_floor_bps(100.0) == pytest.approx(1.0)
    # A cheap name cannot be quoted tighter than one tick, whatever the
    # high-low estimator says about its range.
    assert costs.spread_bps(0.2, 5.0) == pytest.approx(20.0)
    # A wide estimate above the tick is left alone.
    assert costs.spread_bps(40.0, 5.0) == pytest.approx(40.0)
    with pytest.raises(ValueError):
        costs.tick_floor_bps(0.0)


def test_impact_rises_monotonically_with_participation():
    previous = -1.0
    for participation in (1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 0.5):
        value = costs.athl_impact_bps(0.02, participation)
        assert value > previous
        previous = value
    # Concave in size, as the published exponents require: doubling the order
    # must cost less than twice as much per share, or the model is not ATHL.
    single = costs.athl_impact_bps(0.02, 0.005)
    double = costs.athl_impact_bps(0.02, 0.010)
    assert double < 2 * single


def test_impact_is_linear_in_volatility():
    assert costs.athl_impact_bps(0.04, 0.005) == pytest.approx(
        2 * costs.athl_impact_bps(0.02, 0.005)
    )


def test_amihud_floor_takes_effect_and_can_only_raise_the_cost():
    """The conservatism rule: the larger of two documented estimators."""
    base = dict(
        price=20.0,
        adv20_dollar=5.0e6,
        sigma=0.02,
        cs_spread_bps=8.0,
        usable_pairs=21,
    )
    modelled = costs.estimate_cost(
        costs.CostInputs(amihud_lambda_bps_per_musd=0.0, **base), 25_000.0
    )
    floored = costs.estimate_cost(
        costs.CostInputs(amihud_lambda_bps_per_musd=500.0, **base), 25_000.0
    )
    assert modelled is not None and floored is not None
    assert modelled.impact_bps == modelled.athl_impact_bps
    assert floored.impact_bps == floored.amihud_impact_bps
    assert floored.round_trip_bps > modelled.round_trip_bps
    # The floor never LOWERS a cost: taking the max is the whole rule, and a
    # blend would let a small lambda talk a large modelled impact down.
    assert floored.impact_bps >= floored.athl_impact_bps


def test_participation_cap_truncates_and_reports_rather_than_silently_shrinking():
    inputs = costs.CostInputs(
        price=20.0,
        adv20_dollar=2.0e6,
        sigma=0.015,
        cs_spread_bps=8.0,
        amihud_lambda_bps_per_musd=40.0,
        usable_pairs=21,
    )
    estimate = costs.estimate_cost(inputs, 1_000_000.0)
    assert estimate is not None
    assert estimate.notional_allowed_usd == pytest.approx(20_000.0)
    assert estimate.participation == pytest.approx(costs.ADV_PARTICIPATION_CAP)
    assert estimate.capacity_truncated is True
    assert estimate.truncated_notional_fraction == pytest.approx(0.98)
    # Cost is priced on what could actually be traded, not on what was asked
    # for. Charging impact for the refused 98% would double-count the
    # constraint: the position simply does not exist.
    uncapped = costs.estimate_cost(inputs, 20_000.0)
    assert uncapped is not None
    assert uncapped.round_trip_bps == pytest.approx(estimate.round_trip_bps)
    assert uncapped.capacity_truncated is False


def test_a_window_too_short_to_measure_yields_no_estimate():
    inputs = costs.CostInputs(
        price=20.0,
        adv20_dollar=5.0e6,
        sigma=0.02,
        cs_spread_bps=8.0,
        amihud_lambda_bps_per_musd=10.0,
        usable_pairs=costs.MIN_USABLE_PAIRS - 1,
    )
    assert inputs.is_estimable() is False
    assert costs.estimate_cost(inputs, 25_000.0) is None
    # One more usable pair and it is measurable. The boundary is the declared
    # constant, not an accident of the data.
    ok = dataclasses.replace(inputs, usable_pairs=costs.MIN_USABLE_PAIRS)
    assert costs.estimate_cost(ok, 25_000.0) is not None


@pytest.mark.parametrize(
    "field, value",
    [
        ("price", 0.0),
        ("price", float("nan")),
        ("adv20_dollar", 0.0),
        ("sigma", float("nan")),
        ("cs_spread_bps", -1.0),
        ("amihud_lambda_bps_per_musd", float("inf")),
    ],
)
def test_unusable_inputs_refuse_rather_than_substituting_a_default(field, value):
    payload = dict(
        price=20.0,
        adv20_dollar=5.0e6,
        sigma=0.02,
        cs_spread_bps=8.0,
        amihud_lambda_bps_per_musd=10.0,
        usable_pairs=21,
    )
    payload[field] = value
    assert costs.estimate_cost(costs.CostInputs(**payload), 25_000.0) is None


# -- the estimators, from raw bars ---------------------------------------------


def _bars(sessions: int, *, spread_fraction: float = 0.01) -> tuple[list, list, list, list]:
    """A synthetic tape with a known high-low range and a drifting close."""
    closes, highs, lows, volumes = [], [], [], []
    price = 20.0
    for index in range(sessions):
        price *= 1.001 if index % 2 else 0.999
        closes.append(price)
        highs.append(price * (1 + spread_fraction))
        lows.append(price * (1 - spread_fraction))
        volumes.append(50_000.0)
    return highs, lows, closes, volumes


def test_corwin_schultz_floors_the_window_mean_once_never_each_pair():
    """Per-pair flooring is a truncation bias, and it is why band gradients
    invert on a real archive. The estimator must average raw pair estimates."""
    highs, lows, closes, _volumes = _bars(22)
    spread, pairs = costs.corwin_schultz_spread_bps(highs, lows, closes)
    assert pairs == 21
    assert spread is not None and spread >= 0.0

    # A tape that gaps overnight by more than it ranges intraday drives most
    # two-day estimates negative — the same 40% of pairs a real cross-section
    # produces. A handful of wide-range sessions keep the honest window mean
    # positive, so this compares two positive numbers rather than one number
    # against a floor.
    gappy_high, gappy_low = [], []
    for index in range(22):
        price = 20.0 * 1.006 if index % 2 else 20.0
        gappy_high.append(price * 1.002)
        gappy_low.append(price * 0.998)
    for index in (5, 6, 7, 14, 15, 16):
        price = gappy_high[index] / 1.002
        gappy_high[index] = price * 1.02
        gappy_low[index] = price * 0.98
    # Each session closes at the end of its range that the NEXT session gaps
    # away from, so the overnight-gap adjustment cannot absorb the gap and the
    # two-day span stays wide. Closes at the other end would let the shift
    # bring the ranges back on top of each other, and the tape would stop
    # producing the negative pair estimates this test is about.
    gappy_close = [
        gappy_high[index] if index % 2 == 0 else gappy_low[index]
        for index in range(22)
    ]

    raw_estimates = _raw_pair_estimates(gappy_high, gappy_low, gappy_close)
    assert sum(1 for value in raw_estimates if value < 0) > len(raw_estimates) // 3

    window_floored, _ = costs.corwin_schultz_spread_bps(
        gappy_high, gappy_low, gappy_close
    )
    per_pair = 1e4 * sum(max(value, 0.0) for value in raw_estimates) / len(raw_estimates)
    assert window_floored is not None and window_floored > 0.0
    assert per_pair > 2 * window_floored, (
        "per-pair flooring must read materially WIDER than window-mean flooring; "
        "if it does not, the test tape no longer exercises the distinction"
    )


def _raw_pair_estimates(highs, lows, closes) -> list[float]:
    """Unfloored two-day estimates, so the test can see the negatives that the
    rejected treatment would truncate away.

    Gap-adjusted, like the estimator under test: this helper exists to isolate
    the FLOORING rule, so it must differ from the implementation in the
    flooring and in nothing else.
    """
    denominator = 3.0 - 2.0 * math.sqrt(2.0)
    estimates = []
    for index in range(len(highs) - 1):
        high_2, low_2 = costs.gap_adjusted_range(
            closes[index], highs[index + 1], lows[index + 1]
        )
        beta = (
            math.log(highs[index] / lows[index]) ** 2
            + math.log(high_2 / low_2) ** 2
        )
        gamma = math.log(max(highs[index], high_2) / min(lows[index], low_2)) ** 2
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / denominator - math.sqrt(
            gamma / denominator
        )
        estimates.append(2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)))
    return estimates


#: A tape that gaps away from the previous close every session, with each
#: close near the end of its own range that the next session gaps away from.
#: Adjusted, the two sessions' ranges very nearly coincide and the estimate is
#: strongly positive; unadjusted, the overnight move is read as intraday range,
#: which inflates gamma, shrinks alpha and floors the window at zero.
_GAPPED_TAPE = (
    # (low, high, close)
    (20.00, 20.40, 20.05),
    (20.65, 21.05, 20.70),
    (21.30, 21.70, 21.65),
    (20.90, 21.30, 20.95),
    (21.55, 21.95, 21.90),
    (21.10, 21.50, 21.15),
    (21.75, 22.15, 21.95),
    (21.80, 22.20, 21.85),
    (22.45, 22.85, 22.80),
    (22.05, 22.45, 22.10),
    (22.70, 23.10, 22.75),
    (23.35, 23.75, 23.50),
)


def test_the_overnight_gap_adjustment_is_applied_and_is_not_cosmetic():
    """The regression for the adjustment this module's docstring promises.

    The estimator took highs and lows only, so the paper's overnight-gap
    adjustment could not be applied and was not, while the module docstring,
    docs/COST-MODEL.md and the split-exclusion rationale all claimed it. The
    omission is one-sided: an overnight gap inflates gamma, shrinks alpha and
    shrinks the spread, so the public methodology of record reported a NARROWER
    spread than the model it documents — the optimistic direction, largest in
    exactly the gappy thin names the model exists to price.
    """
    highs = [row[1] for row in _GAPPED_TAPE]
    lows = [row[0] for row in _GAPPED_TAPE]
    closes = [row[2] for row in _GAPPED_TAPE]

    adjusted, pairs = costs.corwin_schultz_spread_bps(highs, lows, closes)
    unadjusted, _ = costs.corwin_schultz_spread_bps(
        highs, lows, closes, gap_adjust=False
    )
    assert pairs == 11
    assert unadjusted == 0.0
    assert adjusted is not None and adjusted > 100.0
    assert adjusted > unadjusted

    # And the shift itself, in all three branches.
    assert costs.gap_adjusted_range(20.05, 21.05, 20.65) == pytest.approx((20.45, 20.05))
    assert costs.gap_adjusted_range(21.65, 21.30, 20.90) == pytest.approx((21.65, 21.25))
    assert costs.gap_adjusted_range(20.95, 21.30, 20.90) == (21.30, 20.90)


def test_closes_are_required_by_the_spread_estimator():
    """A caller that cannot supply closes cannot silently get the unadjusted
    estimate back: the adjustment is part of the model, not an option."""
    highs = [row[1] for row in _GAPPED_TAPE]
    lows = [row[0] for row in _GAPPED_TAPE]
    with pytest.raises(TypeError):
        costs.corwin_schultz_spread_bps(highs, lows)


@pytest.mark.parametrize(
    "vector", costs.BAR_GOLDEN_VECTORS, ids=lambda v: v["name"]
)
def test_raw_bar_golden_vector_is_reproduced(vector: dict):
    """The pin, one level below the composition vectors.

    Every entry in GOLDEN_VECTORS supplies ``cs_spread_bps`` as a scalar INPUT,
    so none of them touches the four estimators that produce it: two
    implementations could disagree about the overnight-gap adjustment, the
    flooring rule or the usable-pair definition and still reproduce all seven.
    These vectors take raw bars.
    """
    bars = vector["bars"]
    excluded = vector["excluded_pairs"]
    spread, pairs = costs.corwin_schultz_spread_bps(
        bars["highs"], bars["lows"], bars["closes"], excluded_pairs=excluded
    )
    if vector["expected"] is None:
        assert spread is None, f"{vector['name']} must refuse: {vector['why']}"
        assert (
            costs.cost_inputs_from_bars(
                price=bars["closes"][-1],
                adv20_dollar=5.0e6,
                highs=bars["highs"],
                lows=bars["lows"],
                closes=bars["closes"],
                volumes=bars["volumes"],
                excluded_pairs=excluded,
            )
            is None
        )
        return
    sigma, _ = costs.close_to_close_sigma(bars["closes"], excluded_pairs=excluded)
    lam, _ = costs.amihud_lambda_bps_per_musd(
        bars["closes"], bars["volumes"], excluded_pairs=excluded
    )
    expected = vector["expected"]
    assert pairs == expected["usable_pairs"]
    assert spread == pytest.approx(expected["cs_spread_bps"], rel=1e-9, abs=1e-12)
    assert sigma == pytest.approx(expected["sigma"], rel=1e-9, abs=1e-12)
    assert lam == pytest.approx(
        expected["amihud_lambda_bps_per_musd"], rel=1e-9, abs=1e-12
    )

    # The vector also records what the SAME tape produces without the gap
    # adjustment, so an implementation that omits it fails here rather than
    # quietly agreeing on a narrower spread.
    unadjusted, _ = costs.corwin_schultz_spread_bps(
        bars["highs"],
        bars["lows"],
        bars["closes"],
        excluded_pairs=excluded,
        gap_adjust=False,
    )
    assert unadjusted == pytest.approx(
        vector["unadjusted_cs_spread_bps"], rel=1e-9, abs=1e-12
    )


def test_the_bar_vectors_cover_what_the_composition_vectors_cannot():
    """The blind spot this file was missing, asserted rather than assumed."""
    for vector in costs.GOLDEN_VECTORS:
        assert "cs_spread_bps" in vector["inputs"], (
            "composition vectors take the spread as an input; if one ever "
            "measured it, this test is stale"
        )
    names = {vector["name"] for vector in costs.BAR_GOLDEN_VECTORS}
    assert {"gapped-window", "contained-closes", "bar-window-short-refusal"} <= names
    gapped = next(v for v in costs.BAR_GOLDEN_VECTORS if v["name"] == "gapped-window")
    assert gapped["unadjusted_cs_spread_bps"] != gapped["expected"]["cs_spread_bps"], (
        "the gapped vector must DISCRIMINATE: an implementation without the "
        "overnight-gap adjustment has to fail it"
    )
    contained = next(
        v for v in costs.BAR_GOLDEN_VECTORS if v["name"] == "contained-closes"
    )
    assert contained["unadjusted_cs_spread_bps"] == contained["expected"]["cs_spread_bps"]


def test_every_bar_vector_field_declares_the_units_a_porting_repo_must_convert_to():
    """The reason ``bar_vectors`` sat on this side of the wall for a day.

    The block was added here in #35 and never landed in Stock-Vault, and a
    literal port would not have worked if it had: the vault's Amihud estimator
    returns |r| / $volume as a fraction per dollar, this file's field is bps of
    price per $1M traded, and the 1e10 between them was written down nowhere.
    A pin the other repository cannot implement without guessing is not a pin.
    """
    contract = costs._golden_payload()["estimator_contract"]
    declared = set(contract["units"])
    for vector in costs.BAR_GOLDEN_VECTORS:
        fields = set(vector["expected"] or ()) | {"unadjusted_cs_spread_bps"}
        missing = fields - declared
        assert not missing, f"{vector['name']} publishes undeclared field(s) {sorted(missing)}"
    # And the conversion itself is stated, not left as folklore.
    assert "1e10" in contract["units"]["amihud_lambda_bps_per_musd"]
    # And the third convention, which is not a unit but is just as unguessable:
    # a per-PAIR mask is not a per-DATE exclusion, and reading it as one moves
    # usable_pairs by a whole pair.
    assert "BOTH pairs touching it" in contract["excluded_pairs"]


def test_the_contract_says_which_layer_owns_the_refusal():
    """The second thing a porting repo cannot guess.

    ``MIN_USABLE_PAIRS`` is enforced inside ``corwin_schultz_spread_bps`` here
    and one level up, in ``estimate()``, in Stock-Vault. Driving the refusal
    vector through the bare estimator on that side returns a NUMBER, so an
    implementer following the vector literally would conclude the two repos
    disagree when they do not.
    """
    contract = costs._golden_payload()["estimator_contract"]
    assert "MIN_USABLE_PAIRS" in contract["refusal_layer"]
    assert "composed" in contract["refusal_layer"].lower()
    refusals = [v for v in costs.BAR_GOLDEN_VECTORS if v["expected"] is None]
    assert refusals, "the contract describes a refusal layer; a vector must exercise it"


def test_the_pin_note_states_its_mechanism_at_the_strength_it_actually_holds():
    """The false attestation, as a regression.

    The note asserted, as fact, that "both repositories carry a byte-identical
    copy" — while they did not, and while nothing in either suite could have
    told. Under attestations-computed-never-asserted that sentence is the
    failure mode the pin exists to prevent, published in a public repository.
    The note must describe the check that runs, and must not claim an identity
    neither side can verify from where it stands.
    """
    note = costs._golden_payload()["note"]
    assert "byte-identical copy and both assert its sha256" not in note
    # It has to say where the cross-repository comparison actually happens, and
    # that it only runs in one direction.
    assert "Stock-Vault's CI" in note
    assert "one-directional" in note


def test_estimators_refuse_a_short_window_rather_than_reporting_a_thin_number():
    highs, lows, closes, volumes = _bars(8)
    assert costs.corwin_schultz_spread_bps(highs, lows, closes) == (None, 7)
    assert costs.close_to_close_sigma(closes) == (None, 7)
    assert costs.amihud_lambda_bps_per_musd(closes, volumes) == (None, 7)
    assert (
        costs.cost_inputs_from_bars(
            price=20.0,
            adv20_dollar=1e6,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
        )
        is None
    )


def test_excluded_pairs_are_dropped_from_every_estimator_together():
    """A split left in the window is not noise, it is corruption: a -50%
    session and a gap the Corwin-Schultz adjustment will 'correct' by shifting a
    whole day's range. Excluding it from sigma but not from the spread would
    leave the two terms describing different tapes."""
    highs, lows, closes, _volumes = _bars(24)
    closes = list(closes)
    closes[12] = closes[12] / 2.0  # a 2:1 split, unadjusted
    highs[12] = highs[12] / 2.0
    lows[12] = lows[12] / 2.0

    excluded = [False] * (len(closes) - 1)
    excluded[11] = True  # the pair spanning the ex-date
    excluded[12] = True

    dirty, _ = costs.close_to_close_sigma(closes)
    clean, pairs = costs.close_to_close_sigma(closes, excluded_pairs=excluded)
    assert dirty is not None and clean is not None
    assert pairs == len(closes) - 3
    assert clean < dirty / 5, "an unexcluded split must dominate the volatility"


def test_cost_inputs_from_bars_and_the_composed_estimate_agree_with_the_terms():
    highs, lows, closes, volumes = _bars(22)
    inputs = costs.cost_inputs_from_bars(
        price=closes[-1],
        adv20_dollar=1.0e6,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes,
    )
    assert inputs is not None
    estimate = costs.estimate_cost(inputs, 5_000.0)
    assert estimate is not None
    assert estimate.spread_bps == costs.spread_bps(inputs.cs_spread_bps, inputs.price)
    assert estimate.half_spread_bps == estimate.spread_bps / 2
    assert estimate.impact_bps == max(
        costs.athl_impact_bps(inputs.sigma, estimate.participation),
        costs.amihud_impact_bps(inputs.amihud_lambda_bps_per_musd, estimate.notional_allowed_usd),
    )
    assert estimate.round_trip_bps == pytest.approx(2 * estimate.one_way_bps)


def test_amihud_lambda_uses_the_median_so_one_thin_session_cannot_price_the_name():
    highs, lows, closes, volumes = _bars(24)
    del highs, lows
    typical, _ = costs.amihud_lambda_bps_per_musd(closes, volumes)
    volumes[7] = 1.0  # one near-zero-volume print
    with_outlier, _ = costs.amihud_lambda_bps_per_musd(closes, volumes)
    assert typical is not None and with_outlier is not None
    assert with_outlier == pytest.approx(typical, rel=0.25)
