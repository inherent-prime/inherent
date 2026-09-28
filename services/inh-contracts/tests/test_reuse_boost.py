"""Usage-based ranking boost math tests (inherent#394)."""

import math

from inh_contracts.reuse_boost import (
    DEFAULT_BOOST_CAP,
    apply_reuse_boost,
    reuse_boost_multiplier,
)


def test_zero_reuse_count_is_unboosted():
    assert reuse_boost_multiplier(reuse_count=0, weight=0.5) == 1.0
    assert apply_reuse_boost(score=0.8, reuse_count=0, weight=0.5) == 0.8


def test_zero_weight_is_unboosted_even_with_reuse():
    assert reuse_boost_multiplier(reuse_count=100, weight=0.0) == 1.0
    assert apply_reuse_boost(score=0.8, reuse_count=100, weight=0.0) == 0.8


def test_negative_reuse_count_is_unboosted():
    # Defensive: reuse_count should never be negative in practice, but the
    # formula must not produce a NEGATIVE (score-reducing) multiplier here.
    assert reuse_boost_multiplier(reuse_count=-1, weight=0.5) == 1.0


def test_boost_increases_score_when_enabled():
    boosted = apply_reuse_boost(score=0.8, reuse_count=3, weight=0.1)
    assert boosted > 0.8


def test_boost_matches_formula_below_cap():
    weight = 0.05
    reuse_count = 2
    expected_multiplier = 1.0 + weight * math.log1p(reuse_count)
    assert expected_multiplier < DEFAULT_BOOST_CAP  # sanity: this case shouldn't hit the cap
    assert reuse_boost_multiplier(reuse_count, weight) == expected_multiplier
    assert apply_reuse_boost(1.0, reuse_count, weight) == expected_multiplier


def test_boost_is_hard_capped():
    # A huge reuse_count with a large weight would otherwise blow past any
    # reasonable multiplier -- the cap must hold regardless.
    assert reuse_boost_multiplier(reuse_count=100_000, weight=1.0) == DEFAULT_BOOST_CAP
    assert apply_reuse_boost(score=1.0, reuse_count=100_000, weight=1.0) == DEFAULT_BOOST_CAP


def test_custom_cap_is_respected():
    assert reuse_boost_multiplier(reuse_count=100_000, weight=1.0, cap=1.2) == 1.2


def test_more_reuse_boosts_more_but_with_diminishing_returns():
    m1 = reuse_boost_multiplier(reuse_count=1, weight=0.1)
    m2 = reuse_boost_multiplier(reuse_count=2, weight=0.1)
    m10 = reuse_boost_multiplier(reuse_count=10, weight=0.1)
    m11 = reuse_boost_multiplier(reuse_count=11, weight=0.1)
    assert 1.0 < m1 < m2 < m10 < m11
    # log1p diminishing returns: the 1->2 jump is bigger than the 10->11 jump.
    assert (m2 - m1) > (m11 - m10)
