"""Portfolio state: JSON schema, load/validate/save, booking and P&L.

The state file is committed back to the repo by the Actions workflow and
may be hand-edited by the user (e.g. if a signalled trade wasn't executed,
or was filled at a different price), so loading is strict about structure
but tolerant about formatting, and derived values are always recomputed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

import pandas as pd

from .config import Config
from . import costs

SCHEMA_VERSION = 1


@dataclass
class Position:
    ticker: str
    market: str                 # "UK" | "US"
    shares: int
    entry_date: str             # ISO date
    entry_price_gbp: float      # per share, GBP
    entry_cost_gbp: float       # commission + stamp/FX + spread paid on entry

    @property
    def entry_value_gbp(self) -> float:
        return self.shares * self.entry_price_gbp

    def days_held(self, today: date) -> int:
        return (today - date.fromisoformat(self.entry_date)).days

    def unrealised_pnl_gbp(self, price_gbp: float) -> float:
        return self.shares * price_gbp - self.entry_value_gbp - self.entry_cost_gbp


@dataclass
class Trade:
    """A signalled trade. Sized in whole shares at the day's close."""
    side: str                   # "BUY" | "SELL"
    ticker: str
    market: str
    shares: int
    price_gbp: float
    value_gbp: float
    est_cost_gbp: float
    reason: str


@dataclass
class PortfolioState:
    cash_gbp: float
    positions: list[Position] = field(default_factory=list)
    trade_log: list[dict] = field(default_factory=list)
    last_run: str | None = None
    schema_version: int = SCHEMA_VERSION

    def position_for(self, ticker: str) -> Position | None:
        return next((p for p in self.positions if p.ticker == ticker), None)

    def held_tickers(self) -> list[str]:
        return [p.ticker for p in self.positions]

    def n_us_positions(self) -> int:
        return sum(1 for p in self.positions if p.market == "US")

    def equity_gbp(self, prices: pd.Series) -> float:
        """Mark-to-market total equity. Raises KeyError if a holding has
        no price — callers must have validated data first."""
        value = sum(p.shares * float(prices[p.ticker]) for p in self.positions)
        return self.cash_gbp + value


class StateError(RuntimeError):
    pass


def load_state(path: Path) -> PortfolioState:
    if not path.exists():
        raise StateError(f"state file missing: {path} — seed it before running")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise StateError(f"state file is not valid JSON ({e}) — fix {path}") from e

    if raw.get("schema_version", 1) != SCHEMA_VERSION:
        raise StateError(f"unsupported schema_version in {path}")

    try:
        positions = [
            Position(
                ticker=str(p["ticker"]),
                market=str(p["market"]),
                shares=int(p["shares"]),
                entry_date=str(p["entry_date"]),
                entry_price_gbp=float(p["entry_price_gbp"]),
                entry_cost_gbp=float(p.get("entry_cost_gbp", 0.0)),
            )
            for p in raw.get("positions", [])
        ]
        state = PortfolioState(
            cash_gbp=float(raw["cash_gbp"]),
            positions=positions,
            trade_log=list(raw.get("trade_log", [])),
            last_run=raw.get("last_run"),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise StateError(f"malformed state in {path}: {e}") from e

    for p in state.positions:
        if p.market not in ("UK", "US"):
            raise StateError(f"position {p.ticker} has bad market {p.market!r}")
        if p.shares <= 0 or p.entry_price_gbp <= 0:
            raise StateError(f"position {p.ticker} has non-positive shares/price")
        date.fromisoformat(p.entry_date)  # raises on garbage

    if state.cash_gbp < 0:
        raise StateError("cash_gbp is negative")
    return state


def save_state(state: PortfolioState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": state.schema_version,
        "cash_gbp": round(state.cash_gbp, 2),
        "positions": [
            {**asdict(p), "entry_value_gbp": round(p.entry_value_gbp, 2)}
            for p in state.positions
        ],
        "last_run": state.last_run,
        "trade_log": state.trade_log[-200:],  # keep the file bounded
    }
    path.write_text(json.dumps(payload, indent=1))


def size_buy(
    ticker: str,
    market: str,
    price_gbp: float,
    budget_gbp: float,
    cfg: Config,
) -> Trade | None:
    """Whole-share BUY sized so value + costs fit within budget.
    Returns None if even one share doesn't fit."""
    # positive-form check so NaN (all comparisons False) is rejected too
    if not (price_gbp > 0) or not (budget_gbp > 0):
        return None
    shares = int(budget_gbp / price_gbp)
    while shares > 0:
        value = shares * price_gbp
        cost = costs.buy_cost_gbp(market, value, cfg)
        if value + cost <= budget_gbp:
            return Trade("BUY", ticker, market, shares, price_gbp, value, cost, "")
        shares -= 1
    return None


def make_sell(position: Position, price_gbp: float, cfg: Config, reason: str) -> Trade:
    value = position.shares * price_gbp
    cost = costs.sell_cost_gbp(position.market, value, cfg)
    return Trade("SELL", position.ticker, position.market, position.shares,
                 price_gbp, value, cost, reason)


def apply_trades(state: PortfolioState, trades: list[Trade], today: date) -> PortfolioState:
    """Book signalled trades at their quoted close prices + modelled costs.
    Sells are booked before buys so freed cash funds new entries."""
    for t in sorted(trades, key=lambda t: 0 if t.side == "SELL" else 1):
        if t.side == "SELL":
            pos = state.position_for(t.ticker)
            if pos is None:
                raise StateError(f"SELL for {t.ticker} but no such position")
            state.cash_gbp += t.value_gbp - t.est_cost_gbp
            state.positions.remove(pos)
        elif t.side == "BUY":
            if state.position_for(t.ticker) is not None:
                raise StateError(f"BUY for {t.ticker} but already held")
            spend = t.value_gbp + t.est_cost_gbp
            if spend > state.cash_gbp + 0.01:
                raise StateError(
                    f"BUY {t.ticker} needs £{spend:.2f} but cash is £{state.cash_gbp:.2f}"
                )
            state.cash_gbp -= spend
            state.positions.append(
                Position(
                    ticker=t.ticker,
                    market=t.market,
                    shares=t.shares,
                    entry_date=today.isoformat(),
                    entry_price_gbp=t.price_gbp,
                    entry_cost_gbp=t.est_cost_gbp,
                )
            )
        else:
            raise StateError(f"unknown trade side {t.side!r}")
        state.trade_log.append(
            {
                "date": today.isoformat(),
                "side": t.side,
                "ticker": t.ticker,
                "shares": t.shares,
                "price_gbp": round(t.price_gbp, 4),
                "value_gbp": round(t.value_gbp, 2),
                "cost_gbp": round(t.est_cost_gbp, 2),
                "reason": t.reason,
            }
        )
    return state
