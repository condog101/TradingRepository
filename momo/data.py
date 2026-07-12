"""Market data layer: batched yfinance downloads with retries, Stooq
gap-fill, GBp->GBP normalisation and coverage fail-safes.

The cardinal rule: bad or partial data must never generate a trade signal.
If coverage is poor or a *current holding* has no fresh price, we raise
DataError and the runner tells Telegram instead of trading.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from .config import Config

log = logging.getLogger(__name__)


class DataError(RuntimeError):
    """Raised when data is too incomplete to trade on."""


@dataclass
class MarketData:
    closes_gbp: pd.DataFrame        # daily adjusted closes, GBP
    traded_value_gbp: pd.DataFrame  # close * volume, GBP
    gbpusd: float                   # USD per 1 GBP
    missing: list[str] = field(default_factory=list)
    coverage: float = 1.0
    notes: list[str] = field(default_factory=list)


# --- yfinance fetching -----------------------------------------------------

def _yf_download_batch(tickers: list[str], cfg: Config, period: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One batched download; returns (closes, volumes) in native currency."""
    import yfinance as yf

    df = yf.download(
        tickers=" ".join(tickers),
        period=period,
        interval="1d",
        auto_adjust=True,
        threads=False,
        progress=False,
        group_by="column",
    )
    if df is None or df.empty:
        raise RuntimeError("yfinance returned empty frame")
    if isinstance(df.columns, pd.MultiIndex):
        closes = df["Close"]
        volumes = df["Volume"]
    else:  # single ticker: flat columns
        closes = df[["Close"]].rename(columns={"Close": tickers[0]})
        volumes = df[["Volume"]].rename(columns={"Volume": tickers[0]})
    return closes, volumes


def fetch_history(tickers: list[str], cfg: Config, period: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Batched, retried download of (closes, volumes) for many tickers."""
    closes_parts, volume_parts = [], []
    for i in range(0, len(tickers), cfg.yf_batch_size):
        batch = tickers[i : i + cfg.yf_batch_size]
        last_err: Exception | None = None
        for attempt in range(cfg.yf_max_retries):
            try:
                c, v = _yf_download_batch(batch, cfg, period)
                closes_parts.append(c)
                volume_parts.append(v)
                last_err = None
                break
            except Exception as e:  # yfinance throws all sorts
                last_err = e
                wait = 2 ** attempt + random.random()
                log.warning("batch %d attempt %d failed (%s); retrying in %.1fs",
                            i // cfg.yf_batch_size, attempt + 1, e, wait)
                time.sleep(wait)
        if last_err is not None:
            log.error("batch starting %s failed after retries: %s", batch[0], last_err)
    if not closes_parts:
        raise DataError("all yfinance batches failed")
    closes = pd.concat(closes_parts, axis=1)
    volumes = pd.concat(volume_parts, axis=1)
    # drop duplicate columns if a ticker appeared in two batches
    closes = closes.loc[:, ~closes.columns.duplicated()]
    volumes = volumes.loc[:, ~volumes.columns.duplicated()]
    return closes, volumes


def _stooq_fill(missing: list[str], closes: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, list[str]]:
    """Gap-fill still-missing tickers from Stooq (unadjusted — fallback only)."""
    from pandas_datareader import data as pdr

    still_missing = []
    for t in missing:
        stooq_sym = t[:-2] + ".UK" if t.endswith(".L") else t
        try:
            df = pdr.DataReader(stooq_sym, "stooq")
            if df.empty:
                raise ValueError("empty")
            s = df["Close"].sort_index()
            closes[t] = s.reindex(closes.index)
            log.info("stooq filled %s", t)
        except Exception:
            still_missing.append(t)
    return closes, still_missing


# --- currency handling ------------------------------------------------------

# Yahoo currency code -> multiplier to GBP (USD handled separately via FX)
def _currency_cache_path(cfg: Config) -> Path:
    return Path(cfg.state_dir) / "currencies.json"


def load_currency_cache(cfg: Config) -> dict[str, str]:
    p = _currency_cache_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_currency_cache(cache: dict[str, str], cfg: Config) -> None:
    p = _currency_cache_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=1, sort_keys=True))


def verify_currencies(tickers: list[str], cfg: Config) -> dict[str, str]:
    """Look up Yahoo's quote currency for a *small* set of sizing-critical
    tickers (holdings + buy candidates), using a persistent cache so each
    name costs one metadata call ever."""
    import yfinance as yf

    cache = load_currency_cache(cfg)
    changed = False
    for t in tickers:
        if t in cache:
            continue
        try:
            cur = yf.Ticker(t).fast_info["currency"]
            cache[t] = str(cur)
            changed = True
        except Exception:
            log.warning("currency lookup failed for %s; using market default", t)
    if changed:
        save_currency_cache(cache, cfg)
    return cache


def to_gbp(
    frame: pd.DataFrame,
    markets: dict[str, str],
    gbpusd: float | pd.Series,
    currencies: dict[str, str],
) -> pd.DataFrame:
    """Convert a per-ticker frame from native quote currency to GBP.

    Defaults: US tickers are USD; .L tickers are GBp (pence). Known
    exceptions come from the verified `currencies` map. `gbpusd` may be a
    scalar or a daily series (USD per GBP) aligned to the frame's index —
    a series makes US momentum reflect GBP-terms returns, matching what a
    GBP-based account actually experiences.
    """
    if isinstance(gbpusd, pd.Series):
        usd_divisor = gbpusd.reindex(frame.index).ffill().bfill()
    else:
        usd_divisor = gbpusd

    out = frame.copy()
    for t in out.columns:
        cur = currencies.get(t)
        if cur is None:
            cur = "USD" if markets.get(t) == "US" else "GBp"
        cur_lower = cur.lower()
        if cur_lower == "gbp":
            pass
        elif cur_lower == "gbx" or cur == "GBp":
            out[t] = out[t] * 0.01
        elif cur_lower == "usd":
            out[t] = out[t] / usd_divisor
        else:
            log.warning("unhandled currency %s for %s; leaving unconverted", cur, t)
    return out


# --- FX ----------------------------------------------------------------------

def fetch_gbpusd_series(cfg: Config, period: str) -> pd.Series:
    """Daily GBPUSD closes (USD per GBP) over `period`."""
    import yfinance as yf

    for attempt in range(cfg.yf_max_retries):
        try:
            df = yf.download("GBPUSD=X", period=period, interval="1d",
                             progress=False, auto_adjust=True)
            s = df["Close"].dropna()
            if isinstance(s, pd.DataFrame):  # yfinance sometimes MultiIndexes
                s = s.iloc[:, 0]
            rate = float(s.iloc[-1])
            if not (0.9 < rate < 2.5):
                raise ValueError(f"implausible GBPUSD {rate}")
            return s
        except Exception as e:
            log.warning("GBPUSD fetch attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    raise DataError("could not fetch GBPUSD rate — aborting, US names cannot be valued")


# --- top-level ---------------------------------------------------------------

def load_market_data(
    tickers: dict[str, str],
    holdings: list[str],
    cfg: Config,
    today: date | None = None,
) -> MarketData:
    """Fetch, normalise and validate market data for the whole universe.

    Raises DataError when the result is not safe to trade on.
    """
    today = today or date.today()
    notes: list[str] = []
    all_tickers = sorted(tickers)
    period = f"{cfg.price_period_months}mo"

    closes, volumes = fetch_history(all_tickers, cfg, period)

    fetched = [t for t in all_tickers if t in closes.columns and closes[t].notna().any()]
    missing = [t for t in all_tickers if t not in fetched]
    if missing:
        closes, missing = _stooq_fill(missing, closes, cfg)
        for t in list(missing):
            if t in closes.columns and closes[t].notna().any():
                missing.remove(t)
    if missing:
        notes.append(f"{len(missing)} tickers have no data: {', '.join(missing[:10])}"
                     + ("…" if len(missing) > 10 else ""))

    coverage = 1 - len(missing) / max(len(all_tickers), 1)
    if coverage < cfg.min_universe_coverage:
        raise DataError(
            f"only {coverage:.0%} of the universe has data "
            f"(minimum {cfg.min_universe_coverage:.0%}) — refusing to trade on partial data"
        )

    missing_holdings = [h for h in holdings if h not in closes.columns or closes[h].dropna().empty]
    if missing_holdings:
        raise DataError(
            f"no fresh price for current holding(s) {missing_holdings} — refusing to run"
        )

    fx = fetch_gbpusd_series(cfg, period)
    gbpusd = float(fx.iloc[-1])

    # Verify quote currency only for sizing-critical names (holdings now;
    # buy candidates get verified in the runner before sizing).
    currencies = verify_currencies(holdings, cfg)

    closes_gbp = to_gbp(closes, tickers, fx, currencies)
    volumes = volumes.reindex(columns=closes.columns)
    traded_value_gbp = closes_gbp * volumes

    return MarketData(
        closes_gbp=closes_gbp,
        traded_value_gbp=traded_value_gbp,
        gbpusd=gbpusd,
        missing=missing,
        coverage=coverage,
        notes=notes,
    )
