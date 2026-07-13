"""Hysteresis rotation engine. Pure: (momentum table, state, config, date)
in, list of Trades out. Shared verbatim by the live runner and the backtest.

Decision rules, in order:

1. Risk exits — ANY weekday, ignore min-hold:
     hard stop (close >= hard_stop_pct below entry, GBP terms)
     trend break (close < sma_exit_buffer * 200dma)
2. Rank exits — rotation day only, min-hold required:
     holding's rank decayed past sell_rank, or holding lost data-quality
3. Proactive swap — rotation day only, at most ONE per week:
     worst still-held holding replaced by a top-buy_rank candidate, but only
     if the momentum edge clears the cost gate (swap_safety_factor x costs)
4. Entries — rotation day only:
     free slots filled from the top of the eligible ranking (rank <= buy_rank),
     respecting the US position cap and whole-share sizing

Being slow to buy is cheap; being slow to sell is not — hence the asymmetry.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd

from .config import Config
from . import costs
from .portfolio import PortfolioState, Trade, make_sell, size_buy


def is_rotation_day(today: date, cfg: Config) -> bool:
    return today.weekday() == cfg.rotation_weekday


def target_position_value(equity_gbp: float, cfg: Config) -> float:
    return equity_gbp * (1 - cfg.cash_buffer_pct) / cfg.n_positions


def propose_trades(
    table: pd.DataFrame,
    state: PortfolioState,
    cfg: Config,
    today: date,
    rotation_day: bool | None = None,
) -> list[Trade]:
    if rotation_day is None:
        rotation_day = is_rotation_day(today, cfg)

    missing = [p.ticker for p in state.positions if p.ticker not in table.index]
    if missing:
        raise ValueError(f"holdings missing from momentum table: {missing}")

    prices = table["price"]
    trades: list[Trade] = []
    remaining = list(state.positions)

    def exit_position(pos, reason: str) -> None:
        trades.append(make_sell(pos, float(prices[pos.ticker]), cfg, reason))
        remaining.remove(pos)

    # --- 1. risk exits (daily) ---
    for pos in list(remaining):
        px = float(prices[pos.ticker])
        row = table.loc[pos.ticker]
        stop_level = pos.entry_price_gbp * (1 - cfg.hard_stop_pct)
        if px <= stop_level:
            exit_position(pos, f"hard stop: {px:.2f} <= {stop_level:.2f} "
                               f"(-{cfg.hard_stop_pct:.0%} from entry)")
        elif not math.isnan(row["sma"]) and px < cfg.sma_exit_buffer * row["sma"]:
            exit_position(pos, f"trend break: close {px:.2f} < "
                               f"{cfg.sma_exit_buffer:.0%} of 200dma {row['sma']:.2f}")

    if not rotation_day:
        return trades

    # --- 2. rank exits (weekly, min-hold respected) ---
    for pos in list(remaining):
        if pos.days_held(today) < cfg.min_holding_days:
            continue
        row = table.loc[pos.ticker]
        rank = row["rank"]
        if math.isnan(rank):
            exit_position(pos, "lost data-quality/liquidity eligibility")
        elif rank > cfg.sell_rank:
            exit_position(pos, f"rank decayed to {int(rank)} (> {cfg.sell_rank})")

    # --- candidate list: top-ranked, eligible, not held ---
    held = {p.ticker for p in remaining}
    cands = table[
        table["eligible_buy"]
        & (table["rank"] <= cfg.buy_rank)
        & ~table.index.isin(held)
    ].sort_values("rank")

    equity = state.equity_gbp(prices)
    pos_value = target_position_value(equity, cfg)

    # --- 3. at most one proactive swap per rotation day ---
    if cfg.n_positions - len(remaining) == 0 and not cands.empty:
        swappable = [p for p in remaining if p.days_held(today) >= cfg.min_holding_days]
        if swappable:
            worst = max(swappable, key=lambda p: table.loc[p.ticker, "rank"])
            worst_row = table.loc[worst.ticker]
            best = cands.iloc[0]
            edge = float(best["mom_ann"]) - float(worst_row["mom_ann"])
            # a holding that still ranks well is never swap fodder —
            # swapping rank-7 for rank-1 is churn, not signal
            swap_floor = max(cfg.swap_out_rank, cfg.buy_rank)
            if worst_row["rank"] > swap_floor and costs.swap_clears_gate(
                    edge, str(best["market"]), worst.market, pos_value, cfg):
                exit_position(worst, f"swapped out: rank {int(worst_row['rank'])}, "
                                     f"edge {edge:.0%}/yr clears cost gate")

    # --- 4. entries into free slots ---
    cash = state.cash_gbp + sum(t.value_gbp - t.est_cost_gbp for t in trades if t.side == "SELL")
    n_us = sum(1 for p in remaining if p.market == "US")
    slots = cfg.n_positions - len(remaining)
    buffer_gbp = equity * cfg.cash_buffer_pct

    for ticker, row in cands.iterrows():
        if slots <= 0:
            break
        market = str(row["market"])
        if market == "US" and n_us >= cfg.max_us_positions:
            continue
        budget = min(pos_value, cash - buffer_gbp)
        buy = size_buy(str(ticker), str(market), float(row["price"]), budget, cfg)
        if buy is None:
            continue
        buy.reason = f"entered top {cfg.buy_rank} (rank {int(row['rank'])})"
        trades.append(buy)
        cash -= buy.value_gbp + buy.est_cost_gbp
        slots -= 1
        if market == "US":
            n_us += 1

    return trades
