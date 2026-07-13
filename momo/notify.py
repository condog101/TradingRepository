"""Telegram notification: message formatting + delivery.

Credentials come from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars
(GitHub Actions secrets). TELEGRAM_CHAT_ID is optional: if only the token
is set, the chat id is discovered from getUpdates — which works as long
as the owner has sent the bot at least one message. Without a token,
messages fall back to stdout so local dry runs need no setup.
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from .portfolio import PortfolioState, Trade

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"

# Discovered chat id is persisted so it survives the command poller acking
# getUpdates (which empties the update history discovery relies on).
CHAT_ID_FILE = Path("state/telegram_chat.json")


def load_saved_chat_id() -> str | None:
    try:
        return str(json.loads(CHAT_ID_FILE.read_text())["chat_id"])
    except Exception:
        return None


def save_chat_id(chat_id: str) -> None:
    try:
        CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHAT_ID_FILE.write_text(json.dumps({"chat_id": str(chat_id)}))
    except OSError:
        log.warning("could not persist chat id", exc_info=True)


def discover_chat_id(token: str) -> str | None:
    """Find the owner's chat id from the bot's pending updates. Works when
    the owner has messaged the bot at least once (getUpdates only retains
    recent messages, but a private chat id never changes once seen)."""
    try:
        r = requests.get(UPDATES_API.format(token=token), timeout=30)
        r.raise_for_status()
        for update in reversed(r.json().get("result", [])):
            msg = update.get("message") or update.get("edited_message") or {}
            chat = msg.get("chat", {})
            if chat.get("type") == "private" and "id" in chat:
                return str(chat["id"])
    except requests.RequestException as e:
        log.warning("chat-id discovery failed: %s", e)
    return None


def resolve_chat_id(token: str) -> str | None:
    """Env var > persisted file > getUpdates discovery (persisted on success)."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or load_saved_chat_id()
    if not chat_id:
        chat_id = discover_chat_id(token)
        if chat_id:
            save_chat_id(chat_id)
    return chat_id


def send(text: str) -> bool:
    """Send `text` (HTML) to Telegram; returns True on success.
    Falls back to stdout when credentials are absent."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = resolve_chat_id(token) if token else None
    if not token or not chat_id:
        if token and not chat_id:
            log.warning("no TELEGRAM_CHAT_ID and discovery found no messages — "
                        "send the bot a message once and re-run")
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
            f"  {html.escape(p.ticker)} ({p.market})  {p.shares} sh  {_money(value)}  "
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
