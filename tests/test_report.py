import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from momo.config import Config
from momo.momentum import momentum_table
from momo.portfolio import PortfolioState, Position
from momo import report

from tests.test_backtest import synthetic_market

CFG = Config()
TODAY = date(2026, 7, 13)


@pytest.fixture
def market():
    markets, closes, traded = synthetic_market()
    table = momentum_table(closes, traded, markets, 1000.0, CFG)
    return markets, closes, table


@pytest.fixture
def state(market):
    _, closes, _ = market
    px = closes.ffill().iloc[-1]
    return PortfolioState(
        cash_gbp=2900.0,
        positions=[
            Position("T2.L", "UK", 9, "2026-05-01", float(px["T2.L"]) * 0.9, 10.2),
            Position("T1", "US", 7, "2026-06-20", float(px["T1"]) * 1.05, 12.1),
        ],
        trade_log=[
            {"date": "2026-05-01", "side": "BUY", "ticker": "T2.L",
             "value_gbp": 900.0, "cost_gbp": 10.2, "reason": "entered top 8"},
            {"date": "2026-06-20", "side": "BUY", "ticker": "T1",
             "value_gbp": 950.0, "cost_gbp": 12.1, "reason": "entered top 8"},
        ],
    )


def test_history_append_is_idempotent(tmp_path: Path):
    p = tmp_path / "history.csv"
    report.append_history(p, date(2026, 7, 10), 5000.0, 1000.0, 4, 9200.0)
    report.append_history(p, date(2026, 7, 13), 5100.0, 900.0, 5, 9300.0)
    # same-date rerun replaces, not duplicates
    df = report.append_history(p, date(2026, 7, 13), 5150.0, 950.0, 5, 9310.0)
    assert len(df) == 2
    assert float(df.iloc[-1]["equity_gbp"]) == 5150.0


def test_history_survives_missing_benchmark(tmp_path: Path):
    p = tmp_path / "history.csv"
    df = report.append_history(p, TODAY, 5000.0, 1000.0, 4, None)
    assert pd.isna(df.iloc[-1]["benchmark_close"])
    assert len(report.load_history(p)) == 1


def test_build_data_schema_and_json(market, state, tmp_path: Path):
    markets, closes, table = market
    hist = pd.DataFrame({
        "date": ["2026-07-10", "2026-07-13"],
        "equity_gbp": [5000.0, 5100.0],
        "cash_gbp": [1000.0, 900.0],
        "n_positions": [4, 5],
        "benchmark_close": [9200.0, 9300.0],
    })
    data = report.build_data(state, table, closes, hist, CFG, TODAY,
                             names={"T2.L": "Test Two"})

    for key in ("generated_at", "equity", "cash", "total_return_pct", "stats",
                "positions", "leaderboard", "history", "trades", "config"):
        assert key in data

    assert len(data["positions"]) == 2
    pos = next(p for p in data["positions"] if p["ticker"] == "T2.L")
    assert pos["name"] == "Test Two"
    assert pos["pnl_pct"] > 0            # entry was 10% below current price
    assert pos["stop"] == pytest.approx(pos["entry_price"] * 0.8)
    assert 30 <= len(pos["prices"]) <= 70   # ~600 daily rows -> last 280 -> weekly
    assert data["stats"]["total_costs"] == pytest.approx(22.3)
    assert data["stats"]["n_trades"] == 2

    # leaderboard: top N plus all holdings, held flags set
    held_rows = [r for r in data["leaderboard"] if r["held"]]
    assert {r["ticker"] for r in held_rows} == {"T2.L", "T1"}
    assert all(r["rank"] <= CFG.leaderboard_size or r["held"]
               for r in data["leaderboard"])

    # must serialise strictly (no NaN) — this is what the browser parses
    out = tmp_path / "data.json"
    report.write_site_data(data, out)
    parsed = json.loads(out.read_text())
    assert parsed["equity"] == data["equity"]


def test_drawdown_stats():
    eq = pd.Series([100.0, 110.0, 99.0, 104.5],
                   index=pd.to_datetime(["2026-01-01", "2026-02-01",
                                         "2026-03-01", "2026-04-01"]))
    dd_now, dd_max = report._drawdown_stats(eq)
    assert dd_max == pytest.approx(-0.1)          # 110 -> 99
    assert dd_now == pytest.approx(104.5 / 110 - 1)


def test_change_over_days():
    hist = pd.DataFrame({
        "date": ["2026-06-01", "2026-06-20", "2026-07-13"],
        "equity_gbp": [5000.0, 5200.0, 5250.0],
        "cash_gbp": [0, 0, 0], "n_positions": [5, 5, 5],
        "benchmark_close": [0, 0, 0],
    })
    # 30 days before 2026-07-13 is 2026-06-13 -> base row 2026-06-01
    assert report._change_over_days(hist, 30, TODAY) == pytest.approx(0.05)
    assert report._change_over_days(hist.iloc[:1], 30, TODAY) is None
