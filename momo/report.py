"""Dashboard data: daily equity history and the site/data.json payload
rendered by site/index.html on GitHub Pages.

Everything here is offline-computable from objects the daily run already
has (state, momentum table, GBP closes), so it is fully unit-testable.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .config import Config
from .portfolio import PortfolioState

HISTORY_COLUMNS = ["date", "equity_gbp", "cash_gbp", "n_positions", "benchmark_close"]


# --- daily equity history -----------------------------------------------------

def load_history(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    df = pd.read_csv(path)
    missing = [c for c in HISTORY_COLUMNS if c not in df.columns]
    for c in missing:
        df[c] = float("nan")
    return df[HISTORY_COLUMNS]


def append_history(
    path: Path,
    today: date,
    equity_gbp: float,
    cash_gbp: float,
    n_positions: int,
    benchmark_close: float | None,
) -> pd.DataFrame:
    """Append today's snapshot; re-running on the same date replaces the row."""
    df = load_history(path)
    df = df[df["date"] != today.isoformat()]
    row = pd.DataFrame(
        [{
            "date": today.isoformat(),
            "equity_gbp": round(equity_gbp, 2),
            "cash_gbp": round(cash_gbp, 2),
            "n_positions": n_positions,
            "benchmark_close": round(benchmark_close, 2) if benchmark_close else float("nan"),
        }]
    )
    df = pd.concat([df, row], ignore_index=True).sort_values("date")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


# --- stats ---------------------------------------------------------------------

def _drawdown_stats(equity: pd.Series) -> tuple[float, float]:
    """(current drawdown, max drawdown) as negative fractions; 0.0 if flat."""
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = equity / peak - 1
    return float(dd.iloc[-1]), float(dd.min())


def _change_over_days(history: pd.DataFrame, days: int, today: date) -> float | None:
    """Equity change vs the closest snapshot >= `days` calendar days back."""
    if len(history) < 2:
        return None
    dates = pd.to_datetime(history["date"])
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=days)
    older = history[dates <= cutoff]
    base = older.iloc[-1] if not older.empty else history.iloc[0]
    if base["equity_gbp"] == 0:
        return None
    return float(history["equity_gbp"].iloc[-1] / base["equity_gbp"] - 1)


# --- series helpers ------------------------------------------------------------

def _weekly(series: pd.Series) -> list[list]:
    """Downsample a daily series to weekly closes as [iso_date, value] pairs."""
    s = series.dropna()
    if s.empty:
        return []
    w = s.resample("W-FRI").last().dropna()
    # keep the very latest daily point so the chart is never a week stale
    if not w.empty and s.index[-1] > w.index[-1]:
        w = pd.concat([w, s.iloc[[-1]]])
    return [[d.date().isoformat(), round(float(v), 4)] for d, v in w.items()]


def _clean(x):
    """NaN -> None so the payload is valid JSON."""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


# --- payload -------------------------------------------------------------------

def build_data(
    state: PortfolioState,
    table: pd.DataFrame,
    closes_gbp: pd.DataFrame,
    history: pd.DataFrame,
    cfg: Config,
    today: date,
    names: dict[str, str] | None = None,
) -> dict:
    names = names or {}
    prices = table["price"]
    equity = state.equity_gbp(prices)
    sma = closes_gbp.rolling(cfg.sma_window).mean()

    positions = []
    for p in state.positions:
        px = float(prices[p.ticker])
        row = table.loc[p.ticker]
        stop = p.entry_price_gbp * (1 - cfg.hard_stop_pct)
        sma_now = float(row["sma"]) if pd.notna(row["sma"]) else None
        positions.append({
            "ticker": p.ticker,
            "name": names.get(p.ticker, ""),
            "market": p.market,
            "shares": p.shares,
            "entry_date": p.entry_date,
            "entry_price": round(p.entry_price_gbp, 4),
            "price": round(px, 4),
            "value": round(p.shares * px, 2),
            "pnl_pct": _clean(round(p.unrealised_pnl_gbp(px) / p.entry_value_gbp, 4)),
            "days_held": p.days_held(today),
            "rank": _clean(int(row["rank"]) if pd.notna(row["rank"]) else float("nan")),
            "stop": round(stop, 4),
            "sma": _clean(round(sma_now, 4) if sma_now else float("nan")),
            "dist_to_stop_pct": _clean(round(px / stop - 1, 4)),
            "dist_to_sma_pct": _clean(round(px / sma_now - 1, 4) if sma_now else float("nan")),
            "prices": _weekly(closes_gbp[p.ticker].iloc[-280:]),
            "sma_series": _weekly(sma[p.ticker].iloc[-280:]),
        })

    lead = table[table["rank"].notna()].sort_values("rank")
    held = set(state.held_tickers())
    lead = lead[(lead["rank"] <= cfg.leaderboard_size) | lead.index.isin(held)]
    leaderboard = [
        {
            "ticker": t,
            "name": names.get(t, ""),
            "rank": int(r["rank"]),
            "score": _clean(round(float(r["score"]), 3)),
            "r6": _clean(round(float(r["r6"]), 4)),
            "r12": _clean(round(float(r["r12"]), 4)),
            "above_sma": bool(r["above_sma"]),
            "eligible": bool(r["eligible_buy"]),
            "held": t in held,
        }
        for t, r in lead.iterrows()
    ]

    hist_equity = pd.Series(
        history["equity_gbp"].values,
        index=pd.to_datetime(history["date"]),
        dtype=float,
    ) if len(history) else pd.Series(dtype=float)
    dd_now, dd_max = _drawdown_stats(hist_equity)

    total_costs = round(sum(t.get("cost_gbp", 0.0) for t in state.trade_log), 2)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": today.isoformat(),
        "starting_cash": cfg.starting_cash_gbp,
        "equity": round(equity, 2),
        "cash": round(state.cash_gbp, 2),
        "total_return_pct": round(equity / cfg.starting_cash_gbp - 1, 4),
        "stats": {
            "drawdown_pct": round(dd_now, 4),
            "max_drawdown_pct": round(dd_max, 4),
            "change_30d_pct": _clean(_change_over_days(history, 30, today) or float("nan")),
            "total_costs": total_costs,
            "n_trades": len(state.trade_log),
        },
        "positions": positions,
        "leaderboard": leaderboard,
        "history": [
            {k: _clean(v) for k, v in rec.items()}
            for rec in history.to_dict(orient="records")
        ],
        "trades": list(reversed(state.trade_log[-50:])),
        "config": {
            "n_positions": cfg.n_positions,
            "buy_rank": cfg.buy_rank,
            "sell_rank": cfg.sell_rank,
            "hard_stop_pct": cfg.hard_stop_pct,
            "min_holding_days": cfg.min_holding_days,
        },
    }


def write_site_data(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, allow_nan=False))
