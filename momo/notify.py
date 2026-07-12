"""Telegram notification: message formatting + delivery.

Credentials come from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars
(GitHub Actions secrets). Without them, messages fall back to stdout so
local dry runs need no setup.
"""

from __future__ import annotations

import html
import logging
import os
import time
from datetime import date

import pandas as pd
import requests

from .portfolio import PortfolioState, Trade

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str) -> bool:
    """Send `text` (HTML) to Telegram; returns True on success.
    Falls back to stdout when credentials are absent."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("--- telegram (no credentials, printing) ---")
        print(text)
        return False

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for attempt in (1, 2):
        try:
            r = requests.post(API.format(token=token), json=payload, timeout=30)
            if r.status_code == 200:
                return True
            log.warning("telegram %s: %s", r.status_code, r.text[:200])
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3)
                continue
            return False
        except requests.RequestException as e:
            log.warning("telegram attempt %d failed: %s", attempt, e)
            time.sleep(3)
    return False


# --- formatting ---------------------------------------------------------------


def _money(x: float) -> str:
    return f"£{x:,.2f}"


def _portfolio_lines(state: PortfolioState, prices: pd.Series,
                     table: pd.DataFrame, today: date) -> list[str]:
    lines = []
    for p in state.positions:
        px = float(prices[p.ticker])
        value = p.shares * px
        pnl = p.unrealised_pnl_gbp(px)
        pnl_pct = pnl / p.entry_value_gbp if p.entry_value_gbp else 0
        rank = table.loc[p.ticker, "rank"] if p.ticker in table.index else float("nan")
        rank_s = f"#{int(rank)}" if pd.notna(rank) else "–"
        lines.append(
            f"  {html.escape(p.ticker)} ({p.market})  {_money(value)}  "
            f"{pnl_pct:+.1%}  {p.days_held(today)}d  rank {rank_s}"
        )
    if not state.positions:
        lines.append("  (all cash)")
    return lines


def _footer(state: PortfolioState, prices: pd.Series, starting: float) -> list[str]:
    equity = state.equity_gbp(prices)
    total = equity / starting - 1
    return [
        "",
        f"<b>Equity {_money(equity)}</b> ({total:+.1%} vs {_money(starting)}) · "
        f"cash {_money(state.cash_gbp)}",
    ]


def format_trades(trades: list[Trade], state: PortfolioState, prices: pd.Series,
                  table: pd.DataFrame, today: date, starting: float,
                  notes: list[str]) -> str:
    lines = [f"📈 <b>TRADES TO MAKE — {today.isoformat()}</b>", ""]
    for t in trades:
        emoji = "🟢 BUY " if t.side == "BUY" else "🔴 SELL"
        lines.append(
            f"{emoji} <b>{html.escape(t.ticker)}</b> ({t.market}) — "
            f"{t.shares} shares ≈ {_money(t.value_gbp)}"
        )
        lines.append(f"     @ ~{_money(t.price_gbp)}/sh · est. costs {_money(t.est_cost_gbp)}")
        lines.append(f"     <i>{html.escape(t.reason)}</i>")
    lines.append("")
    lines.append("Execute manually on Interactive Investor at next opportunity.")
    lines.append("If you don't execute (or fill differs a lot), edit state/portfolio.json.")
    lines.append("")
    lines.append("<b>Portfolio after booking:</b>")
    lines += _portfolio_lines(state, prices, table, today)
    lines += _footer(state, prices, starting)
    for n in notes:
        lines.append(f"⚠️ {html.escape(n)}")
    return "\n".join(lines)


def format_heartbeat(state: PortfolioState, prices: pd.Series, table: pd.DataFrame,
                     today: date, starting: float, notes: list[str]) -> str:
    lines = [f"✅ <b>No trades today — {today.isoformat()}</b>", "",
             "<b>Portfolio:</b>"]
    lines += _portfolio_lines(state, prices, table, today)
    lines += _footer(state, prices, starting)
    for n in notes:
        lines.append(f"⚠️ {html.escape(n)}")
    return "\n".join(lines)


def format_error(stage: str, err: Exception) -> str:
    return (
        f"🚨 <b>Signal run FAILED</b> at stage: {html.escape(stage)}\n"
        f"<pre>{html.escape(str(err)[:600])}</pre>\n"
        f"No trades were signalled. Check the Actions log."
    )
