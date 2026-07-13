"""Momentum scoring, filters and ranking. Pure functions — no I/O.

Score design:
  raw momentum = 0.5 * annualised 6m return + 0.5 * 12m return,
                 both measured up to `skip_days` ago (1-month reversal skip)
  drag         = amortised II round-trip cost for the name's market
  score        = (raw momentum - drag) / annualised vol

Ranking runs over every name passing the *data-quality* filters
(completeness, liquidity, penny floor). The *trend* filters (above 200dma,
positive 6m return) additionally gate new buys only — a holding that slips
below its SMA is handled by the exit logic, not by vanishing from the table.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config
from . import costs

VOL_FLOOR = 0.10  # annualised; stops near-zero-vol names dominating the ratio


def momentum_table(
    closes_gbp: pd.DataFrame,
    traded_value_gbp: pd.DataFrame,
    markets: dict[str, str],
    position_value_gbp: float,
    cfg: Config,
) -> pd.DataFrame:
    """Score every ticker as of the last row of `closes_gbp`.

    Returns a DataFrame indexed by ticker with columns:
      price, r6, r12, mom_ann, vol_ann, drag, score, rank,
      quality, above_sma, eligible_buy, median_value_gbp
    """
    c = closes_gbp
    n_rows = len(c)
    needed = cfg.lookback_12m + cfg.skip_days + 1

    # last *known* close: a ticker that didn't trade on the final day must
    # not poison equity/budget maths with NaN (staleness is already policed
    # by the completeness filter)
    price = c.ffill().iloc[-1]

    # returns measured up to skip_days ago
    if n_rows >= needed:
        p_skip = c.iloc[-(cfg.skip_days + 1)]
        p_6m = c.iloc[-(cfg.skip_days + cfg.lookback_6m + 1)]
        p_12m = c.iloc[-(cfg.skip_days + cfg.lookback_12m + 1)]
    else:  # not enough history for anything — all-NaN table
        p_skip = p_6m = p_12m = pd.Series(np.nan, index=c.columns)

    r6 = p_skip / p_6m - 1
    r12 = p_skip / p_12m - 1
    r6_ann = (1 + r6) ** 2 - 1
    mom_ann = 0.5 * r6_ann + 0.5 * r12

    daily_ret = c.pct_change(fill_method=None)
    vol_ann = daily_ret.iloc[-cfg.vol_window :].std() * np.sqrt(252)

    sma = c.iloc[-cfg.sma_window :].mean()
    above_sma = price > sma

    window = c.iloc[-needed:]
    completeness = window.notna().mean()
    median_value = traded_value_gbp.iloc[-cfg.volume_median_window :].median()

    drag = pd.Series(
        {
            t: costs.amortised_cost_drag(markets.get(t, "UK"), position_value_gbp, cfg)
            for t in c.columns
        }
    )
    score = (mom_ann - drag) / vol_ann.clip(lower=VOL_FLOOR)

    quality = (
        (completeness >= cfg.min_completeness)
        & (median_value >= cfg.min_median_daily_value_gbp)
        & (price >= cfg.min_price_gbp)
        & score.notna()
        & vol_ann.notna()
    )

    rank = score.where(quality).rank(ascending=False, method="first")

    eligible_buy = quality & above_sma & (r6 > cfg.min_6m_return)

    return pd.DataFrame(
        {
            "market": pd.Series({t: markets.get(t, "UK") for t in c.columns}),
            "price": price,
            "r6": r6,
            "r12": r12,
            "mom_ann": mom_ann,
            "vol_ann": vol_ann,
            "drag": drag,
            "score": score,
            "rank": rank,
            "quality": quality,
            "above_sma": above_sma,
            "eligible_buy": eligible_buy,
            "median_value_gbp": median_value,
            "sma": sma,
        }
    )
