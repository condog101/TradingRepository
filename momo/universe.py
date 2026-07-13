"""Universe construction: FTSE 100 + FTSE 250 from Wikipedia.

UK-only by design: the account is a Stocks & Shares ISA, which can only
hold GBP cash, so every US trade would pay II's 0.75% FX fee both ways.
(To re-enable US names — e.g. in a GIA with a USD balance — add the S&P
500 Wikipedia page back here; costs/signals remain multi-market capable.)

Constituent churn is slow, so the scraped list is cached in
state/universe.json and only refreshed every ~4 weeks. On scrape failure
the cache is used regardless of age (flagged as stale so the daily
message can mention it).
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from .config import Config

log = logging.getLogger(__name__)

WIKI_PAGES = {
    "UK100": "https://en.wikipedia.org/wiki/FTSE_100_Index",
    "UK250": "https://en.wikipedia.org/wiki/FTSE_250_Index",
}

_HEADERS = {"User-Agent": "momo-signals/1.0 (personal trading-signal tool)"}

# LSE names whose Yahoo quote currency is not GBp — value is a multiplier
# applied to the raw price to get GBP. Extend as oddballs are discovered.
CURRENCY_OVERRIDES: dict[str, float] = {}


@dataclass
class Universe:
    tickers: dict[str, str]     # yahoo ticker -> "UK" | "US"
    names: dict[str, str]       # yahoo ticker -> company name
    refreshed: str              # ISO date of last successful scrape
    stale: bool = False


def _norm_us(symbol: str) -> str:
    # Yahoo uses '-' where the index list uses '.' (BRK.B -> BRK-B)
    return symbol.strip().replace(".", "-")


def _norm_uk(symbol: str) -> str:
    # LSE tickers: strip trailing dots (BT.A -> BT-A), append .L
    s = symbol.strip().rstrip(".")
    s = s.replace(".", "-")
    return f"{s}.L"


def _read_wiki_tables(url: str) -> list[pd.DataFrame]:
    resp = requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def _extract_constituents(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Find the constituents table: the one with a ticker-ish column and a
    company-ish column, and enough rows to be an index list."""
    for t in tables:
        cols = {str(c).strip().lower() for c in t.columns}
        ticker_col = next(
            (c for c in t.columns if str(c).strip().lower() in ("symbol", "ticker", "epic")),
            None,
        )
        name_col = next(
            (c for c in t.columns if str(c).strip().lower() in ("security", "company", "company name")),
            None,
        )
        if ticker_col is not None and name_col is not None and len(t) > 50:
            out = t[[ticker_col, name_col]].copy()
            out.columns = ["symbol", "name"]
            return out
    raise ValueError(f"no constituents table found (saw columns: {cols})")


def scrape_universe() -> Universe:
    tickers: dict[str, str] = {}
    names: dict[str, str] = {}

    for key, url in WIKI_PAGES.items():
        table = _extract_constituents(_read_wiki_tables(url))
        market = "US" if key == "US" else "UK"
        norm = _norm_us if market == "US" else _norm_uk
        count = 0
        for _, row in table.iterrows():
            sym = str(row["symbol"])
            if not sym or sym.lower() == "nan" or not re.match(r"^[A-Za-z0-9.\-]+$", sym.strip()):
                continue
            yt = norm(sym)
            tickers[yt] = market
            names[yt] = str(row["name"]).strip()
            count += 1
        log.info("scraped %s: %d constituents", key, count)
        if count < 50:
            raise ValueError(f"{key} scrape returned only {count} rows — layout change?")

    return Universe(tickers=tickers, names=names, refreshed=date.today().isoformat())


def load_cached(path: Path) -> Universe | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return Universe(
        tickers=raw["tickers"],
        names=raw.get("names", {}),
        refreshed=raw["refreshed"],
    )


def save_cache(uni: Universe, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"tickers": uni.tickers, "names": uni.names, "refreshed": uni.refreshed},
            indent=1,
            sort_keys=True,
        )
    )


def get_universe(cfg: Config, today: date | None = None) -> Universe:
    """Return the cached universe, rescraping if it is older than
    cfg.universe_refresh_days. Never fails hard if a cache exists."""
    today = today or date.today()
    path = Path(cfg.universe_file)
    cached = load_cached(path)

    fresh_enough = (
        cached is not None
        and (today - datetime.fromisoformat(cached.refreshed).date()).days
        < cfg.universe_refresh_days
    )
    if fresh_enough:
        return cached

    try:
        uni = scrape_universe()
        save_cache(uni, path)
        return uni
    except Exception:
        log.exception("universe scrape failed")
        if cached is not None:
            cached.stale = True
            return cached
        raise
