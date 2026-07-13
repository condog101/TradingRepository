"""Command-poller tests — Telegram API fully mocked."""

import dataclasses
import json

import pytest
import requests

from momo import bot_commands, notify
from momo.config import Config


class FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def wire(monkeypatch, tmp_path, updates, last_message="heartbeat text"):
    cfg = dataclasses.replace(Config(), last_message_file=str(tmp_path / "last_message.txt"))
    if last_message is not None:
        (tmp_path / "last_message.txt").write_text(last_message)
    monkeypatch.setattr(bot_commands, "load_config", lambda: cfg)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(notify, "CHAT_ID_FILE", tmp_path / "telegram_chat.json")

    calls = {"get": [], "post": [], "sent": []}

    def fake_get(url, params=None, timeout=None):
        calls["get"].append(params or {})
        if "offset" in (params or {}):
            return FakeResp({"result": []})   # the ack call
        return FakeResp({"result": updates})

    def fake_post(url, json=None, timeout=None):
        calls["post"].append(url)
        if "sendMessage" in url:
            calls["sent"].append(json["text"])
        return FakeResp({})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def upd(uid, chat_id, text, chat_type="private"):
    return {"update_id": uid,
            "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


def test_latest_command_resends_stored_message(monkeypatch, tmp_path):
    calls = wire(monkeypatch, tmp_path, [upd(10, 42, "/latest")])
    assert bot_commands.main() == 0
    assert len(calls["sent"]) == 1
    assert "heartbeat text" in calls["sent"][0]
    # all seen updates acknowledged with max_id + 1
    assert any(p.get("offset") == 11 for p in calls["get"])


def test_status_alias_and_plain_chat_ignored(monkeypatch, tmp_path):
    calls = wire(monkeypatch, tmp_path, [
        upd(1, 42, "hello there"),      # not a command -> ignored
        upd(2, 42, "/status"),
    ])
    assert bot_commands.main() == 0
    assert len(calls["sent"]) == 1


def test_stranger_chat_gets_no_reply(monkeypatch, tmp_path):
    # owner (42) established via persisted file; stranger 666 sends /latest
    (tmp_path / "telegram_chat.json").write_text(json.dumps({"chat_id": "42"}))
    calls = wire(monkeypatch, tmp_path, [upd(5, 666, "/latest")])
    assert bot_commands.main() == 0
    assert calls["sent"] == []
    assert any(p.get("offset") == 6 for p in calls["get"])  # still acked


def test_no_updates_is_a_quiet_noop(monkeypatch, tmp_path):
    calls = wire(monkeypatch, tmp_path, [])
    assert bot_commands.main() == 0
    assert calls["sent"] == []
    assert len(calls["post"]) == 0   # no setMyCommands churn on empty polls


def test_missing_stored_message_explains(monkeypatch, tmp_path):
    calls = wire(monkeypatch, tmp_path, [upd(3, 42, "/latest")], last_message=None)
    assert bot_commands.main() == 0
    assert "No daily update stored yet" in calls["sent"][0]


def test_chat_id_persistence_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "CHAT_ID_FILE", tmp_path / "telegram_chat.json")
    notify.save_chat_id("42")
    assert notify.load_saved_chat_id() == "42"
    # resolve prefers the saved id without touching the network
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(notify, "discover_chat_id",
                        lambda token: pytest.fail("should not discover"))
    assert notify.resolve_chat_id("tok") == "42"
