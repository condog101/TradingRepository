"""Event-loop backtest that drives the SAME decision code as production
(momentum.momentum_table -> signals.propose_trades -> full cost model).

Honesty rules:
  * signals are computed from data up to day t; trades are booked at day
    t+1's close (sells re-priced, buys re-sized) — no lookahead
  * every trade pays the full II cost model
  * risk exits are checked daily, rotations weekly, exactly as live

Known caveat, stated loudly: the universe is TODAY'S constituents, so
results carry survivorship bias and should be read as validation of
turnover, cost drag and relative parameter choices — not as expected CAGR.

Usage (needs network for yfinance/Wikipedia; cached to .price_cache):
  python -m backtest.backtest --years 5
  python -m backtest.backtest --years 5 --sell-rank 100 --top-n 4
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from momo.config import Config
from momo.momentum import momentum_table
from momo.portfolio import PortfolioState, Trade, apply_trades, make_sell, size_buy
from momo.signals import propose_trades, target_position_value
from momo import data as data_mod
from momo import universe as universe_mod

log = logging.getLogger("backtest")


def load_history(cfg: Config, years: int, refresh: bool):
    """Universe + GBP price panel for the backtest, cached to parquet."""
    cache = Path(cfg.cache_dir) / f"backtest_{years}y.parquet"
    uni = universe_mod.get_universe(cfg)
    markets = uni.tickers

    if cache.exists() and not refresh:
        panel = pd.read_parquet(cache)
        closes = panel["close"]
        volumes = panel["volume"]
        fx = panel["fx"].iloc[:, 0].dropna()
        log.info("loaded cached history %s", cache)
    else:
        period = f"{years}y"
        closes, volumes = data_mod.fetch_history(sorted(markets), cfg, period)
        fx = data_mod.fetch_gbpusd_series(cfg, period)
        panel = pd.concat(
            {"close": closes, "volume": volumes, "fx": fx.to_frame("GBPUSD")}, axis=1
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        panel.to_parquet(cache)

    closes_gbp = data_mod.to_gbp(closes, markets, fx, currencies={})
    traded_value = closes_gbp * volumes.reindex(columns=closes_gbp.columns)
    return markets, closes_gbp, traded_value


def execute_intents(
    intents: list[Trade],
    state: PortfolioState,
    prices_now: pd.Series,
    cfg: Config,
    today,
) -> list[Trade]:
    """Re-book yesterday's intents at today's close: sells re-priced, buys
    re-sized. Intents whose ticker has no price today are dropped (sells
    fall back to the last known price — an exit must not silently vanish)."""
    booked: list[Trade] = []
    for t in intents:
        if t.side == "SELL":
            pos = state.position_for(t.ticker)
            if pos is None:
                continue
            px = prices_now.get(t.ticker)
            if px is None or np.isnan(px):
                px = t.price_gbp  # stale, but the exit must happen
            booked.append(make_sell(pos, float(px), cfg, t.reason))

    cash = state.cash_gbp + sum(t.value_gbp - t.est_cost_gbp for t in booked)
    equity = state.equity_gbp(prices_now.reindex(state.held_tickers()).fillna(0))
    pos_value = target_position_value(max(equity, state.cash_gbp), cfg)
    buffer = equity * cfg.cash_buffer_pct

    for t in intents:
        if t.side != "BUY":
            continue
        px = prices_now.get(t.ticker)
        if px is None or np.isnan(px):
            continue
        buy = size_buy(t.ticker, t.market, float(px), min(pos_value, cash - buffer), cfg)
        if buy is None:
            continue
        buy.reason = t.reason
        booked.append(buy)
        cash -= buy.value_gbp + buy.est_cost_gbp
    return booked


def run_backtest(cfg: Config, markets, closes_gbp, traded_value, verbose=False):
    needed = cfg.lookback_12m + cfg.skip_days + 50  # slice depth for the table
    idx = closes_gbp.index
    start = needed + 1
    state = PortfolioState(cash_gbp=cfg.starting_cash_gbp)

    equity_curve = []
    pending: list[Trade] = []
    total_costs = 0.0
    n_trades = 0
    ffilled = closes_gbp.ffill()

    for i in range(start, len(idx)):
        today = idx[i].date()
        prices_now = ffilled.iloc[i]

        # 1. book yesterday's intents at today's close
        if pending:
            booked = execute_intents(pending, state, prices_now, cfg, today)
            state = apply_trades(state, booked, today)
            total_costs += sum(t.est_cost_gbp for t in booked)
            n_trades += len(booked)
            if verbose:
                for t in booked:
                    log.info("%s %s %s %d @ %.2f (%s)", today, t.side, t.ticker,
                             t.shares, t.price_gbp, t.reason)
            pending = []

        # 2. compute today's signals (booked tomorrow)
        rotation = today.weekday() == cfg.rotation_weekday
        window = slice(max(0, i - needed), i + 1)
        if rotation:
            tbl = momentum_table(closes_gbp.iloc[window], traded_value.iloc[window],
                                 markets, target_position_value(
                                     state.equity_gbp(prices_now.reindex(state.held_tickers()).fillna(0)), cfg),
                                 cfg)
            pending = propose_trades(tbl, state, cfg, today, rotation_day=True)
        elif state.positions:
            held = state.held_tickers()
            tbl = momentum_table(closes_gbp[held].iloc[window], traded_value[held].iloc[window],
                                 markets, 1000.0, cfg)
            pending = propose_trades(tbl, state, cfg, today, rotation_day=False)

        equity_curve.append(
            (idx[i], state.equity_gbp(prices_now.reindex(state.held_tickers()).fillna(0)),
             len(state.positions))
        )

    curve = pd.DataFrame(equity_curve, columns=["date", "equity", "n_pos"]).set_index("date")
    return curve, state, total_costs, n_trades


def summarize(curve: pd.DataFrame, state: PortfolioState, total_costs: float,
              n_trades: int, cfg: Config, benchmark: pd.Series | None = None) -> str:
    eq = curve["equity"]
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    daily = eq.pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else float("nan")
    dd = (eq / eq.cummax() - 1).min()
    per_month = n_trades / (years * 12)
    cost_drag = total_costs / eq.iloc[0] / years

    lines = [
        f"period            : {eq.index[0].date()} -> {eq.index[-1].date()} ({years:.1f}y)",
        f"final equity      : £{eq.iloc[-1]:,.0f}  (start £{eq.iloc[0]:,.0f})",
        f"CAGR              : {cagr:+.1%}",
        f"max drawdown      : {dd:.1%}",
        f"Sharpe (daily)    : {sharpe:.2f}",
        f"trades            : {n_trades} total, {per_month:.1f}/month",
        f"total trade costs : £{total_costs:,.0f}  ({cost_drag:.2%}/yr on starting equity)",
        f"avg positions held: {curve['n_pos'].mean():.1f} / {cfg.n_positions}",
    ]
    if benchmark is not None and not benchmark.empty:
        b = benchmark.reindex(eq.index).ffill().dropna()
        if len(b) > 2:
            bench_cagr = (b.iloc[-1] / b.iloc[0]) ** (1 / years) - 1
            lines.append(f"benchmark CAGR    : {bench_cagr:+.1%} (buy & hold, no costs)")
    return "\n".join(lines)


def run_sweep(cfg: Config, markets, closes_gbp, traded_value) -> None:
    """Grid over the swap-gate knobs (the observed turnover drivers) and
    print a compact comparison table."""
    import dataclasses as dc

    print(f"{'swap_factor':>11} {'swap_rank':>9} {'min_hold':>8} | "
          f"{'CAGR':>7} {'maxDD':>7} {'Sharpe':>6} {'tr/mo':>5} {'cost%/yr':>8} {'final £':>8}")
    for factor in (2.0, 3.0, 4.0):
        for out_rank in (8, 20, 40):
            for min_hold in (28, 56):
                c = dc.replace(cfg, swap_safety_factor=factor,
                               swap_out_rank=out_rank, min_holding_days=min_hold)
                curve, state, total_costs, n_trades = run_backtest(
                    c, markets, closes_gbp, traded_value)
                eq = curve["equity"]
                years = (eq.index[-1] - eq.index[0]).days / 365.25
                cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
                daily = eq.pct_change().dropna()
                sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0
                dd = (eq / eq.cummax() - 1).min()
                print(f"{factor:>11.1f} {out_rank:>9d} {min_hold:>8d} | "
                      f"{cagr:>+6.1%} {dd:>7.1%} {sharpe:>6.2f} "
                      f"{n_trades / (years * 12):>5.1f} "
                      f"{total_costs / eq.iloc[0] / years:>8.2%} {eq.iloc[-1]:>8,.0f}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--top-n", type=int, dest="n_positions")
    ap.add_argument("--buy-rank", type=int, dest="buy_rank")
    ap.add_argument("--sell-rank", type=int, dest="sell_rank")
    ap.add_argument("--min-hold", type=int, dest="min_holding_days")
    ap.add_argument("--swap-factor", type=float, dest="swap_safety_factor")
    ap.add_argument("--swap-out-rank", type=int, dest="swap_out_rank")
    ap.add_argument("--sweep", action="store_true",
                    help="run the swap-gate parameter grid and print a table")
    ap.add_argument("--refresh", action="store_true", help="refetch prices")
    ap.add_argument("--verbose", action="store_true", help="log every trade")
    ap.add_argument("--out", default="backtest/equity_curve.csv")
    args = ap.parse_args()

    overrides = {
        k: v for k, v in vars(args).items()
        if k in {f.name for f in dataclasses.fields(Config)} and v is not None
    }
    cfg = dataclasses.replace(Config(), **overrides)

    markets, closes_gbp, traded_value = load_history(cfg, args.years, args.refresh)
    log.info("universe: %d tickers, %d trading days", len(markets), len(closes_gbp))

    if args.sweep:
        run_sweep(cfg, markets, closes_gbp, traded_value)
        return

    curve, state, total_costs, n_trades = run_backtest(
        cfg, markets, closes_gbp, traded_value, verbose=args.verbose
    )

    benchmark = None
    try:
        bench_closes, _ = data_mod.fetch_history(["VWRL.L"], cfg, f"{args.years}y")
        benchmark = bench_closes["VWRL.L"] * 0.01  # GBp -> GBP
    except Exception:
        log.warning("benchmark fetch failed; skipping comparison")

    print("\n=== Backtest summary ===")
    print(summarize(curve, state, total_costs, n_trades, cfg, benchmark))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    curve.to_csv(args.out)
    print(f"\nequity curve written to {args.out}")


if __name__ == "__main__":
    main()
