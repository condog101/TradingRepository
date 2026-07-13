"""End-to-end orchestrator test with the network layer mocked out."""

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest

from momo import run_daily, notify
from momo import data as data_mod
from momo import universe as universe_mod
from momo.config import Config
from momo.data import MarketData
from momo.universe import Universe

from tests.test_backtest import synthetic_market


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Wire run_daily to synthetic data, tmp state paths and a captured
    notify.send. Returns (sent_messages, state_path)."""
    markets, closes, traded = synthetic_market()

    state_path = tmp_path / "portfolio.json"
    state_path.write_text(json.dumps({"cash_gbp": 5000.0, "positions": []}))

    cfg = dataclasses.replace(
        Config(),
        portfolio_file=str(state_path),
        universe_file=str(tmp_path / "universe.json"),
        state_dir=str(tmp_path),
        history_file=str(tmp_path / "history.csv"),
        last_message_file=str(tmp_path / "last_message.txt"),
        site_data_file=str(tmp_path / "site" / "data.json"),
    )
    monkeypatch.setattr(run_daily, "load_config", lambda: cfg)

    monkeypatch.setattr(
        universe_mod, "get_universe",
        lambda cfg, today=None: Universe(
            tickers=markets, names={}, refreshed="2026-07-01"
        ),
    )
    monkeypatch.setattr(
        data_mod, "load_market_data",
        lambda tickers, holdings, cfg, today=None: MarketData(
            closes_gbp=closes, traded_value_gbp=traded, gbpusd=1.30
        ),
    )
    monkeypatch.setattr(data_mod, "verify_currencies", lambda tickers, cfg: {})
    # benchmark fetch for the dashboard history row
    monkeypatch.setattr(
        data_mod, "fetch_history",
        lambda tickers, cfg, period: (
            pd.DataFrame({tickers[0]: [9200.0]},
                         index=pd.to_datetime(["2026-07-10"])),
            pd.DataFrame({tickers[0]: [0.0]},
                         index=pd.to_datetime(["2026-07-10"])),
        ),
    )

    sent = []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    return sent, state_path, cfg


def test_dry_run_sends_but_never_writes(wired, monkeypatch):
    sent, state_path, cfg = wired
    monkeypatch.setenv("DRY_RUN", "1")
    before = state_path.read_text()

    assert run_daily.main() == 0
    assert len(sent) == 1
    assert sent[0].startswith("🧪 [DRY RUN]")
    assert state_path.read_text() == before
    # dashboard outputs must not be written either
    assert not Path(cfg.site_data_file).exists()
    assert not Path(cfg.history_file).exists()


def test_live_run_updates_state_and_pings(wired, monkeypatch):
    sent, state_path, cfg = wired
    monkeypatch.delenv("DRY_RUN", raising=False)

    assert run_daily.main() == 0
    assert len(sent) == 1
    saved = json.loads(state_path.read_text())
    assert saved["last_run"] is not None
    # message is either a trade list (Monday) or a heartbeat — never silence
    assert "TRADES TO MAKE" in sent[0] or "No trades today" in sent[0]

    # dashboard data written and strictly JSON-parseable
    data = json.loads(Path(cfg.site_data_file).read_text())
    assert data["equity"] > 0
    assert data["history"][-1]["benchmark_close"] == 9200.0
    hist = Path(cfg.history_file).read_text().strip().splitlines()
    assert len(hist) == 2  # header + today's row


def test_failure_sends_error_alert(wired, monkeypatch):
    sent, state_path, cfg = wired
    state_path.write_text("{corrupt")

    assert run_daily.main() == 1
    assert len(sent) == 1
    assert "FAILED" in sent[0]
    assert "load state" in sent[0]
