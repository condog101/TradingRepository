"""Interactive Investor execution-cost model (Core plan, post Feb-2026).

All functions are pure. `market` is "UK" or "US". `value_gbp` is the
GBP notional of the trade. Cost components:

  UK buy : commission + 0.5% SDRT + spread
  UK sell: commission + spread
  US buy : commission + 0.75% FX + spread
  US sell: commission + 0.75% FX + spread

The monthly platform fee is a fixed drag independent of trading, so it is
deliberately excluded from trade gating.
"""

from __future__ import annotations

from .config import Config

UK = "UK"
US = "US"


def buy_cost_gbp(market: str, value_gbp: float, cfg: Config) -> float:
    cost = cfg.commission_gbp + cfg.spread_pct * value_gbp
    if market == UK:
        cost += cfg.uk_stamp_duty * value_gbp
    elif market == US:
        cost += cfg.us_fx_fee * value_gbp
    else:
        raise ValueError(f"unknown market: {market!r}")
    return cost


def sell_cost_gbp(market: str, value_gbp: float, cfg: Config) -> float:
    cost = cfg.commission_gbp + cfg.spread_pct * value_gbp
    if market == US:
        cost += cfg.us_fx_fee * value_gbp
    elif market != UK:
        raise ValueError(f"unknown market: {market!r}")
    return cost


def round_trip_cost_gbp(market: str, value_gbp: float, cfg: Config) -> float:
    return buy_cost_gbp(market, value_gbp, cfg) + sell_cost_gbp(market, value_gbp, cfg)


def round_trip_pct(market: str, value_gbp: float, cfg: Config) -> float:
    if value_gbp <= 0:
        return float("inf")
    return round_trip_cost_gbp(market, value_gbp, cfg) / value_gbp


def exit_cost_pct(market: str, value_gbp: float, cfg: Config) -> float:
    if value_gbp <= 0:
        return float("inf")
    return sell_cost_gbp(market, value_gbp, cfg) / value_gbp


def amortised_cost_drag(market: str, value_gbp: float, cfg: Config) -> float:
    """Round-trip cost spread over the expected holding period, as an
    annualised return drag. Subtracted from annualised momentum before
    ranking, so expensive-to-trade (US) names need a bigger edge."""
    return round_trip_pct(market, value_gbp, cfg) / cfg.expected_hold_years


def swap_clears_gate(
    edge_ann_return: float,
    buy_market: str,
    sell_market: str | None,
    value_gbp: float,
    cfg: Config,
) -> bool:
    """True if replacing the held name with the candidate is worth its costs.

    edge_ann_return: candidate's annualised momentum minus the holding's
    (raw return units, not vol-adjusted). The expected extra return over the
    holding period must exceed swap_safety_factor times the total cost of
    doing the swap. Entries into a cash slot (sell_market=None) pay no exit
    cost but must still clear the gate on their own round trip.
    """
    total_cost_pct = round_trip_pct(buy_market, value_gbp, cfg)
    if sell_market is not None:
        total_cost_pct += exit_cost_pct(sell_market, value_gbp, cfg)
    expected_gain = edge_ann_return * cfg.expected_hold_years
    return expected_gain > cfg.swap_safety_factor * total_cost_pct
