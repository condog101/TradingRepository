"""All strategy, cost and schedule parameters in one place.

Every number that shapes a trading decision lives here so the live runner
and the backtest are guaranteed to agree, and so a parameter sweep only
needs to construct Config(...) with overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # --- Portfolio shape ---
    starting_cash_gbp: float = 5_000.0
    n_positions: int = 5
    cash_buffer_pct: float = 0.025  # kept aside for commissions/stamp/FX
    max_us_positions: int = 3       # caps FX-fee exposure and GBPUSD risk

    # --- Momentum score ---
    skip_days: int = 21             # skip most recent month (1-month reversal)
    lookback_6m: int = 126
    lookback_12m: int = 252
    vol_window: int = 126           # daily-return vol window for adjustment
    history_days: int = 273         # 12m lookback + skip: minimum usable history

    # --- Rotation thresholds (hysteresis) ---
    buy_rank: int = 15              # enter only if ranked in the top 15
    sell_rank: int = 80             # exit only once rank decays past 80
    min_holding_days: int = 28      # calendar days before a rank-based exit

    # --- Trend / risk filters ---
    sma_window: int = 200
    sma_exit_buffer: float = 0.98   # exit holding on close < 0.98 * 200dma
    hard_stop_pct: float = 0.20     # exit on close 20% below entry (GBP terms)
    min_6m_return: float = 0.0      # absolute momentum: 6m return must be > 0

    # --- Liquidity / data-quality filters ---
    min_median_daily_value_gbp: float = 5_000_000.0
    volume_median_window: int = 20
    min_completeness: float = 0.95  # fraction of non-NaN closes over history
    min_price_gbp: float = 0.50

    # --- Interactive Investor cost model (Core plan, post Feb-2026) ---
    commission_gbp: float = 3.99
    uk_stamp_duty: float = 0.005    # SDRT on UK buys only
    us_fx_fee: float = 0.0075       # each way (0.75% Core plan)
    spread_pct: float = 0.0015      # half-spread + slippage assumption

    # --- Cost gating ---
    expected_hold_years: float = 0.25   # amortise round trip over ~3 months
    swap_safety_factor: float = 2.0     # swap must clear 2x its total cost

    # --- Cadence ---
    rotation_weekday: int = 0       # 0 = Monday: buys/swaps only on this day
                                    # (exits fire any weekday)

    # --- Data layer ---
    price_period_months: int = 15
    yf_batch_size: int = 100
    yf_max_retries: int = 3
    min_universe_coverage: float = 0.90  # abort below this
    universe_refresh_days: int = 28

    # --- Paths ---
    state_dir: str = "state"
    portfolio_file: str = field(default="state/portfolio.json")
    universe_file: str = field(default="state/universe.json")
    cache_dir: str = ".price_cache"


def load_config() -> Config:
    return Config()


def is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")
