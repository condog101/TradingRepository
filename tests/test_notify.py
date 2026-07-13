"""Telegram delivery tests — all network mocked."""

import requests

from momo import notify


class FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(str(self.status_code))


def test_discover_chat_id_picks_latest_private_chat(monkeypatch):
    payload = {"result": [
        {"message": {"chat": {"id": 111, "type": "private"}, "text": "hi"}},
        {"message": {"chat": {"id": -222, "type": "group"}, "text": "noise"}},
        {"message": {"chat": {"id": 333, "type": "private"}, "text": "later"}},
    ]}
    monkeypatch.setattr(requests, "get", lambda url, timeout: FakeResp(payload))
    assert notify.discover_chat_id("tok") == "333"


def test_discover_chat_id_empty_updates(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: FakeResp({"result": []}))
    assert notify.discover_chat_id("tok") is None


def test_discover_chat_id_network_error(monkeypatch):
    def boom(url, timeout):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    assert notify.discover_chat_id("tok") is None


def test_send_uses_discovered_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(notify, "discover_chat_id", lambda token: "42")
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(json)
        return FakeResp()
    monkeypatch.setattr(requests, "post", fake_post)

    assert notify.send("hello") is True
    assert sent["chat_id"] == "42"
    assert sent["text"] == "hello"


def test_send_without_token_prints(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.send("hello") is False
    assert "hello" in capsys.readouterr().out


def test_send_token_but_no_discoverable_chat(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(notify, "discover_chat_id", lambda token: None)
    assert notify.send("hello") is False
    assert "hello" in capsys.readouterr().out


def test_send_retries_on_5xx(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    calls = []

    def flaky_post(url, json, timeout):
        calls.append(1)
        return FakeResp(status=500) if len(calls) == 1 else FakeResp()
    monkeypatch.setattr(requests, "post", flaky_post)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)

    assert notify.send("hello") is True
    assert len(calls) == 2
