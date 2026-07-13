from datetime import date

import numpy as np
import pandas as pd
import pytest

from momo.config import Config
from momo.momentum import momentum_table
from momo.portfolio import PortfolioState, Position
from momo.signals import is_rotation_day, propose_trades, target_position_value

CFG = Config()
TODAY = date(2026, 7, 13)  # a Monday
OLD = "2026-05-01"         # > 28 days before TODAY
RECENT = "2026-07-01"      # < 28 days before TODAY


def row(market="UK", price=10.0, rank=np.nan, mom=0.5, eligible=False,
        quality=True, above_sma=True, sma=None, vol=0.25):
    return {
        "market": market, "price": price, "r6": mom / 2, "r12": mom,
        "mom_ann": mom, "vol_ann": vol, "drag": 0.06,
        "score": (mom - 0.06) / vol, "rank": rank, "quality": quality,
        "above_sma": above_sma, "eligible_buy": eligible,
        "median_value_gbp": 1e7, "sma": sma if sma is not None else price * 0.8,
    }


def make_table(rows: dict) -> pd.DataFrame:
    return pd.DataFrame.from_dict(rows, orient="index")


def held(ticker, market="UK", price=10.0, entry=OLD, shares=97):
    return Position(ticker, market, shares, entry, price, 10.0)


def test_is_rotation_day():
    assert is_rotation_day(date(2026, 7, 13), CFG)       # Monday
    assert not is_rotation_day(date(2026, 7, 14), CFG)   # Tuesday


def test_quiet_day_no_trades():
    table = make_table({"AAA.L": row(rank=5), "BBB.L": row(rank=1, eligible=True)})
    state = PortfolioState(cash_gbp=100.0, positions=[held("AAA.L")])
    assert propose_trades(table, state, CFG, TODAY, rotation_day=False) == []


def test_hard_stop_fires_any_day_ignoring_min_hold():
    # entry 10.0, price 7.9 -> below the 20% stop at 8.0
    table = make_table({"AAA.L": row(price=7.9, rank=5)})
    state = PortfolioState(cash_gbp=100.0,
                           positions=[held("AAA.L", entry=RECENT)])
    trades = propose_trades(table, state, CFG, TODAY, rotation_day=False)
    assert len(trades) == 1
    assert trades[0].side == "SELL" and "hard stop" in trades[0].reason


def test_trend_break_fires_any_day():
    # price 9.0 < 0.98 * sma 10.0
    table = make_table({"AAA.L": row(price=9.0, sma=10.0, rank=5)})
    state = PortfolioState(cash_gbp=100.0, positions=[held("AAA.L")])
    trades = propose_trades(table, state, CFG, TODAY, rotation_day=False)
    assert len(trades) == 1 and "trend break" in trades[0].reason


def test_rank_exit_only_on_rotation_day_after_min_hold():
    table = make_table({"AAA.L": row(price=10.5, rank=150)})
    fresh = PortfolioState(cash_gbp=100.0, positions=[held("AAA.L", entry=RECENT)])
    aged = PortfolioState(cash_gbp=100.0, positions=[held("AAA.L", entry=OLD)])

    assert propose_trades(table, fresh, CFG, TODAY, rotation_day=True) == []
    assert propose_trades(table, aged, CFG, TODAY, rotation_day=False) == []
    trades = propose_trades(table, aged, CFG, TODAY, rotation_day=True)
    assert len(trades) == 1 and "rank decayed" in trades[0].reason


def test_hysteresis_holds_between_buy_and_sell_rank():
    # rank 20 is outside the buy zone (8) but inside the hold zone (40)
    table = make_table({"AAA.L": row(price=10.5, rank=20)})
    state = PortfolioState(cash_gbp=100.0, positions=[held("AAA.L")])
    assert propose_trades(table, state, CFG, TODAY, rotation_day=True) == []


def test_entries_fill_free_slots_top_rank_first():
    table = make_table({
        "AAA.L": row(rank=1, eligible=True, price=10.0),
        "BBB.L": row(rank=2, eligible=True, price=20.0),
        "CCC.L": row(rank=30, eligible=True, price=5.0),   # outside buy_rank
    })
    state = PortfolioState(cash_gbp=5000.0)
    trades = propose_trades(table, state, CFG, TODAY, rotation_day=True)
    assert [t.ticker for t in trades] == ["AAA.L", "BBB.L"]
    assert all(t.side == "BUY" for t in trades)
    pos_value = target_position_value(5000.0, CFG)
    for t in trades:
        assert t.value_gbp + t.est_cost_gbp <= pos_value + 0.01


def test_no_entries_on_non_rotation_day():
    table = make_table({"AAA.L": row(rank=1, eligible=True)})
    state = PortfolioState(cash_gbp=5000.0)
    assert propose_trades(table, state, CFG, TODAY, rotation_day=False) == []


def test_us_position_cap():
    table = make_table({
        f"US{i}": row(market="US", rank=i + 1, eligible=True) for i in range(5)
    })
    state = PortfolioState(cash_gbp=5000.0)
    trades = propose_trades(table, state, CFG, TODAY, rotation_day=True)
    assert len(trades) == CFG.max_us_positions


def test_unaffordable_share_skipped_for_next_candidate():
    table = make_table({
        "BRK-A": row(market="US", rank=1, eligible=True, price=500_000.0),
        "BBB.L": row(rank=2, eligible=True, price=10.0),
    })
    state = PortfolioState(cash_gbp=1000.0)
    trades = propose_trades(table, state, CFG, TODAY, rotation_day=True)
    assert [t.ticker for t in trades] == ["BBB.L"]


def test_swap_never_evicts_a_holding_still_in_buy_zone():
    positions = [held(f"H{i}.L", entry=OLD) for i in range(5)]
    # all holdings still rank inside the top buy_rank; candidate is stellar
    rows = {f"H{i}.L": row(price=10.5, rank=2 + i, mom=0.10) for i in range(5)}
    rows["NEW.L"] = row(rank=1, eligible=True, mom=0.90)
    table = make_table(rows)
    state = PortfolioState(cash_gbp=50.0, positions=positions)
    assert propose_trades(table, state, CFG, TODAY, rotation_day=True) == []


def test_swap_requires_edge_to_clear_cost_gate():
    def full_book(mom_held):
        positions = [held(f"H{i}.L", entry=OLD) for i in range(5)]
        rows = {f"H{i}.L": row(price=10.5, rank=20 + i, mom=mom_held) for i in range(5)}
        rows["NEW.L"] = row(rank=1, eligible=True, mom=0.60)
        return make_table(rows), PortfolioState(cash_gbp=50.0, positions=positions)

    # edge 0.60-0.50 = 10%/yr: below the ~17% gate for UK->UK at this size
    table, state = full_book(0.50)
    assert propose_trades(table, state, CFG, TODAY, rotation_day=True) == []

    # edge 0.60-0.20 = 40%/yr: clears the gate -> one sell + one buy
    table, state = full_book(0.20)
    trades = propose_trades(table, state, CFG, TODAY, rotation_day=True)
    assert sorted(t.side for t in trades) == ["BUY", "SELL"]
    assert trades[0].ticker == "H4.L" and "swapped out" in trades[0].reason
    assert trades[1].ticker == "NEW.L"


def test_lost_quality_holding_exited_on_rotation_day():
    table = make_table({"AAA.L": row(price=10.5, rank=np.nan, quality=False)})
    state = PortfolioState(cash_gbp=100.0, positions=[held("AAA.L")])
    trades = propose_trades(table, state, CFG, TODAY, rotation_day=True)
    assert len(trades) == 1 and "eligibility" in trades[0].reason


def test_holding_missing_from_table_raises():
    table = make_table({"BBB.L": row(rank=1)})
    state = PortfolioState(cash_gbp=100.0, positions=[held("AAA.L")])
    with pytest.raises(ValueError):
        propose_trades(table, state, CFG, TODAY, rotation_day=True)


# --- momentum_table on synthetic prices -------------------------------------

def synthetic_prices(n_days=300):
    """Three names: strong riser, flat, faller. All liquid."""
    idx = pd.bdate_range(end="2026-07-10", periods=n_days)
    rng = np.random.default_rng(42)
    noise = lambda: rng.normal(0, 0.001, n_days)
    up = 100 * np.exp(np.linspace(0, 0.6, n_days) + noise())
    flat = 100 * np.exp(noise())
    down = 100 * np.exp(np.linspace(0, -0.4, n_days) + noise())
    closes = pd.DataFrame({"UP.L": up, "FLAT.L": flat, "DOWN": down}, index=idx)
    volumes = pd.DataFrame(1e6, index=idx, columns=closes.columns)
    return closes, closes * volumes


def test_momentum_table_ranks_riser_first():
    closes, traded = synthetic_prices()
    markets = {"UP.L": "UK", "FLAT.L": "UK", "DOWN": "US"}
    t = momentum_table(closes, traded, markets, 1000.0, CFG)
    assert t.loc["UP.L", "rank"] == 1.0
    assert t.loc["UP.L", "eligible_buy"]
    assert not t.loc["DOWN", "eligible_buy"]       # below SMA, negative r6
    assert not t.loc["DOWN", "above_sma"]
    assert t.loc["DOWN", "market"] == "US"
    # US name carries a bigger cost drag than UK at the same size
    assert t.loc["DOWN", "drag"] > t.loc["UP.L", "drag"]


def test_momentum_table_insufficient_history_is_ineligible():
    closes, traded = synthetic_prices(n_days=100)   # < 12m + skip
    markets = {c: "UK" for c in closes.columns}
    t = momentum_table(closes, traded, markets, 1000.0, CFG)
    assert not t["eligible_buy"].any()
    assert t["rank"].isna().all()
