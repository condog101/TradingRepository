"""Smoke-test the backtest event loop on a synthetic universe.

Real-data validation runs via the backtest GitHub Actions workflow (this
sandbox has no market-data network access); these tests pin down the
engine mechanics: no lookahead crashes, costs accrue, whole-share sizing,
turnover stays modest on trending data.
"""

import numpy as np
import pandas as pd

from momo.config import Config
from backtest.backtest import run_backtest, summarize

CFG = Config()


def synthetic_market(n_names=40, n_days=600, seed=7):
    """A universe of drifting random walks: a few strong trends, mostly noise."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-10", periods=n_days)
    drifts = np.concatenate([
        rng.uniform(0.0008, 0.002, 6),      # strong risers
        rng.uniform(-0.0015, 0.0008, n_names - 6),
    ])
    closes = {}
    markets = {}
    for j in range(n_names):
        rets = rng.normal(drifts[j], 0.015, n_days)
        name = f"T{j}.L" if j % 2 == 0 else f"T{j}"
        closes[name] = 50 * np.exp(np.cumsum(rets))
        markets[name] = "UK" if name.endswith(".L") else "US"
    closes = pd.DataFrame(closes, index=idx)
    traded_value = closes * 1e6
    return markets, closes, traded_value


def test_backtest_runs_and_trades():
    markets, closes, traded = synthetic_market()
    curve, state, total_costs, n_trades = run_backtest(CFG, markets, closes, traded)

    assert len(curve) > 200
    assert n_trades > 0, "engine never traded on trending data"
    assert total_costs > 0
    assert curve["equity"].iloc[0] > 0
    # equity must always equal cash + holdings (no money leaks): final check
    prices = closes.ffill().iloc[-1]
    recomputed = state.cash_gbp + sum(p.shares * prices[p.ticker] for p in state.positions)
    assert abs(recomputed - curve["equity"].iloc[-1]) < 1.0
    assert 0 <= curve["n_pos"].max() <= CFG.n_positions


def test_backtest_turnover_is_modest_on_trends():
    markets, closes, traded = synthetic_market()
    curve, state, total_costs, n_trades = run_backtest(CFG, markets, closes, traded)
    months = len(curve) / 21
    assert n_trades / months < 4, f"turnover too high: {n_trades} trades in {months:.0f} months"


def test_summarize_formats():
    markets, closes, traded = synthetic_market(n_names=30, n_days=500)
    curve, state, total_costs, n_trades = run_backtest(CFG, markets, closes, traded)
    text = summarize(curve, state, total_costs, n_trades, CFG)
    assert "CAGR" in text and "trades" in text


def test_cash_never_negative():
    markets, closes, traded = synthetic_market(seed=11)
    curve, state, total_costs, n_trades = run_backtest(CFG, markets, closes, traded)
    assert state.cash_gbp >= 0


def test_data_gaps_do_not_crash_the_engine():
    """Regression: a held/candidate ticker with NaN closes on signal days
    once turned equity -> budget -> shares into NaN and crashed size_buy."""
    import numpy as np

    markets, closes, traded = synthetic_market(seed=3)
    rng = np.random.default_rng(0)
    # punch holes: random missing days per ticker, incl. some final rows
    for col in closes.columns[::3]:
        holes = rng.choice(len(closes), size=8, replace=False)
        closes.iloc[holes, closes.columns.get_loc(col)] = np.nan
    closes.iloc[-1, closes.columns.get_loc(closes.columns[0])] = np.nan
    traded = closes * 1e6

    curve, state, total_costs, n_trades = run_backtest(CFG, markets, closes, traded)
    assert len(curve) > 100
    assert state.cash_gbp >= 0
    assert curve["equity"].notna().all()
