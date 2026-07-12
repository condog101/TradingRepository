import math

import pytest

from momo.config import Config
from momo import costs

CFG = Config()


def test_uk_buy_cost_hand_computed():
    # £1,000: 3.99 + 0.5% stamp (5.00) + 0.15% spread (1.50) = 10.49
    assert costs.buy_cost_gbp("UK", 1000, CFG) == pytest.approx(10.49)


def test_uk_sell_cost_hand_computed():
    # £1,000: 3.99 + 1.50 spread = 5.49 (no stamp on sells)
    assert costs.sell_cost_gbp("UK", 1000, CFG) == pytest.approx(5.49)


def test_us_costs_symmetric_fx_both_ways():
    # £1,000: 3.99 + 0.75% FX (7.50) + 1.50 spread = 12.99 each way
    assert costs.buy_cost_gbp("US", 1000, CFG) == pytest.approx(12.99)
    assert costs.sell_cost_gbp("US", 1000, CFG) == pytest.approx(12.99)


def test_round_trip_pct_uk_vs_us():
    uk = costs.round_trip_pct("UK", 1000, CFG)
    us = costs.round_trip_pct("US", 1000, CFG)
    assert uk == pytest.approx(0.01598)   # ~1.6%
    assert us == pytest.approx(0.02598)   # ~2.6%
    assert us > uk


def test_smaller_positions_cost_proportionally_more():
    assert costs.round_trip_pct("UK", 500, CFG) > costs.round_trip_pct("UK", 2000, CFG)


def test_zero_value_is_infinite_pct():
    assert math.isinf(costs.round_trip_pct("UK", 0, CFG))


def test_unknown_market_raises():
    with pytest.raises(ValueError):
        costs.buy_cost_gbp("DE", 1000, CFG)
    with pytest.raises(ValueError):
        costs.sell_cost_gbp("DE", 1000, CFG)


def test_amortised_drag_annualises_round_trip():
    # UK £1,000 round trip ~1.598% amortised over 0.25y -> ~6.4%/yr drag
    drag = costs.amortised_cost_drag("UK", 1000, CFG)
    assert drag == pytest.approx(0.01598 / 0.25)


def test_swap_gate_blocks_marginal_edge():
    # Swapping UK->UK at £1,000 costs ~1.598% + 0.549% = ~2.15%.
    # Gate needs edge * 0.25 > 2 * 2.15% -> edge > ~17.2% annualised.
    assert not costs.swap_clears_gate(0.15, "UK", "UK", 1000, CFG)
    assert costs.swap_clears_gate(0.20, "UK", "UK", 1000, CFG)


def test_cash_slot_entry_cheaper_than_swap():
    # Entering from cash has no exit leg, so a smaller edge clears it.
    edge = 0.15
    assert costs.swap_clears_gate(edge, "UK", None, 1000, CFG)
    assert not costs.swap_clears_gate(edge, "UK", "UK", 1000, CFG)


def test_us_needs_bigger_edge_than_uk():
    # Find an edge that clears UK entry but not US entry from cash.
    edge = 0.15
    assert costs.swap_clears_gate(edge, "UK", None, 1000, CFG)
    assert not costs.swap_clears_gate(edge, "US", None, 1000, CFG)
