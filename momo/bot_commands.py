"""Telegram command poller: answers /latest (and /status) by resending the
most recent daily message.

There is no always-on server — a scheduled GitHub Actions workflow runs this
every ~10 minutes, so replies arrive within roughly 10–15 minutes of sending
the command. Only the owner's chat (the persisted/discovered chat id) gets a
reply; processed updates are acknowledged so they aren't handled twice.

Run as `python -m momo.bot_commands`.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import requests

from .config import load_config
from . import notify

log = logging.getLogger("momo.bot")

COMMANDS_API = "https://api.telegram.org/bot{token}/setMyCommands"
COMMANDS = [
    {"command": "latest", "description": "Resend the latest daily update"},
    {"command": "status", "description": "Same as /latest"},
]


def register_commands(token: str) -> None:
    """Idempotent; makes /latest autocomplete in the Telegram UI."""
    try:
        requests.post(COMMANDS_API.format(token=token),
                      json={"commands": COMMANDS}, timeout=30)
    except requests.RequestException:
        log.warning("setMyCommands failed", exc_info=True)


def fetch_updates(token: str, offset: int | None = None) -> list[dict]:
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(notify.UPDATES_API.format(token=token), params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("result", [])


def latest_message(cfg) -> str:
    p = Path(cfg.last_message_file)
    if p.exists():
        return "🕓 <b>Latest update</b> (resent on request)\n\n" + p.read_text()
    return ("🕓 No daily update stored yet — the first one appears after the "
            "next weekday signal run.")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.info("no TELEGRAM_BOT_TOKEN — nothing to do")
        return 0

    cfg = load_config()
    updates = fetch_updates(token)
    if not updates:
        return 0

    register_commands(token)
    owner = notify.resolve_chat_id(token)  # also persists a newly seen chat id

    replies = 0
    max_id = 0
    for u in updates:
        max_id = max(max_id, int(u.get("update_id", 0)))
        msg = u.get("message") or u.get("edited_message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip().lower()
        if owner and chat_id == owner and text.startswith(("/latest", "/status")):
            notify.send(latest_message(cfg))
            replies += 1

    # acknowledge everything we saw so the next poll starts fresh
    fetch_updates(token, offset=max_id + 1)
    log.info("processed %d update(s), sent %d repl(y/ies)", len(updates), replies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
