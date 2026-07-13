import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from momo.config import Config
from momo.portfolio import (
    PortfolioState,
    Position,
    StateError,
    Trade,
    apply_trades,
    load_state,
    make_sell,
    save_state,
    size_buy,
)

CFG = Config()


def make_state() -> PortfolioState:
    return PortfolioState(
        cash_gbp=3000.0,
        positions=[
            Position("AZN.L", "UK", 8, "2026-06-01", 120.0, 10.4),
            Position("NVDA", "US", 5, "2026-06-15", 140.0, 12.2),
        ],
    )


def test_state_round_trip(tmp_path: Path):
    p = tmp_path / "portfolio.json"
    state = make_state()
    save_state(state, p)
    loaded = load_state(p)
    assert loaded.cash_gbp == 3000.0
    assert loaded.held_tickers() == ["AZN.L", "NVDA"]
    assert loaded.positions[0].entry_price_gbp == 120.0


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(StateError):
        load_state(tmp_path / "nope.json")


def test_load_invalid_json_raises(tmp_path: Path):
    p = tmp_path / "portfolio.json"
    p.write_text("{not json")
    with pytest.raises(StateError):
        load_state(p)


def test_load_rejects_bad_market(tmp_path: Path):
    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps({
        "cash_gbp": 100,
        "positions": [{"ticker": "X", "market": "DE", "shares": 1,
                       "entry_date": "2026-01-01", "entry_price_gbp": 10}],
    }))
    with pytest.raises(StateError):
        load_state(p)


def test_hand_edit_tolerance(tmp_path: Path):
    # ints for floats, missing optional fields, extra whitespace
    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps({
        "cash_gbp": 5000,
        "positions": [{"ticker": "AZN.L", "market": "UK", "shares": 8,
                       "entry_date": "2026-06-01", "entry_price_gbp": 120}],
    }))
    loaded = load_state(p)
    assert loaded.positions[0].entry_cost_gbp == 0.0
    assert loaded.cash_gbp == 5000.0


def test_equity_and_pnl():
    state = make_state()
    prices = pd.Series({"AZN.L": 130.0, "NVDA": 150.0})
    # 8*130 + 5*150 + 3000 = 1040 + 750 + 3000
    assert state.equity_gbp(prices) == pytest.approx(4790.0)
    pos = state.position_for("AZN.L")
    # 8*(130-120) - 10.4 entry cost
    assert pos.unrealised_pnl_gbp(130.0) == pytest.approx(69.6)


def test_size_buy_fits_costs_in_budget():
    t = size_buy("AZN.L", "UK", 120.0, 1000.0, CFG)
    assert t is not None
    assert t.shares == 8
    assert t.value_gbp + t.est_cost_gbp <= 1000.0


def test_size_buy_too_expensive_returns_none():
    assert size_buy("BRK-A", "US", 500_000.0, 1000.0, CFG) is None


def test_size_buy_rejects_nan_price_and_budget():
    nan = float("nan")
    assert size_buy("X.L", "UK", nan, 1000.0, CFG) is None
    assert size_buy("X.L", "UK", 10.0, nan, CFG) is None


def test_apply_trades_sell_before_buy():
    state = PortfolioState(
        cash_gbp=50.0,
        positions=[Position("AZN.L", "UK", 8, "2026-06-01", 120.0, 10.4)],
    )
    sell = make_sell(state.positions[0], 125.0, CFG, "rank decay")
    buy = size_buy("SHEL.L", "UK", 25.0, 900.0, CFG)
    new = apply_trades(state, [buy, sell], date(2026, 7, 13))
    assert new.position_for("AZN.L") is None
    assert new.position_for("SHEL.L") is not None
    assert new.cash_gbp > 0
    assert [e["side"] for e in new.trade_log] == ["SELL", "BUY"]


def test_apply_trades_rejects_overspend():
    state = PortfolioState(cash_gbp=100.0)
    buy = Trade("BUY", "X", "UK", 10, 50.0, 500.0, 10.0, "")
    with pytest.raises(StateError):
        apply_trades(state, [buy], date(2026, 7, 13))


def test_days_held():
    pos = Position("AZN.L", "UK", 8, "2026-06-01", 120.0, 10.4)
    assert pos.days_held(date(2026, 7, 1)) == 30
