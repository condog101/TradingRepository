"""Daily orchestrator: universe -> prices -> scores -> signals -> state -> ping.

Run as `python -m momo.run_daily`. Set DRY_RUN=1 to compute and send the
message without touching state. Any failure sends a Telegram error alert
(if credentials exist) and exits non-zero so the Actions run shows red.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from .config import load_config, is_dry_run
from . import data as data_mod
from . import notify
from . import report
from . import universe as universe_mod
from .momentum import momentum_table
from .portfolio import apply_trades, load_state, save_state
from .signals import propose_trades, target_position_value

log = logging.getLogger("momo")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    dry = is_dry_run()
    today = date.today()
    stage = "startup"

    try:
        stage = "load state"
        state = load_state(Path(cfg.portfolio_file))

        stage = "universe"
        uni = universe_mod.get_universe(cfg)
        tickers = dict(uni.tickers)
        # holdings always stay in the fetch list, even if dropped from the index
        for p in state.positions:
            tickers.setdefault(p.ticker, p.market)

        stage = "market data"
        md = data_mod.load_market_data(tickers, state.held_tickers(), cfg, today)
        notes = list(md.notes)
        if uni.stale:
            notes.append(f"universe list is stale (last scraped {uni.refreshed})")

        stage = "signals"
        # One retry loop: if a proposed BUY turns out to be quoted in an
        # unexpected currency, refresh the cache and recompute once.
        for attempt in range(2):
            prices = md.closes_gbp.ffill().iloc[-1]
            equity = state.equity_gbp(prices)
            pos_value = target_position_value(equity, cfg)
            table = momentum_table(md.closes_gbp, md.traded_value_gbp, tickers,
                                   pos_value, cfg)
            trades = propose_trades(table, state, cfg, today)

            buys = [t.ticker for t in trades if t.side == "BUY"]
            if not buys or attempt == 1:
                break
            before = data_mod.load_currency_cache(cfg)
            verified = data_mod.verify_currencies(buys, cfg)
            surprises = {
                t: verified.get(t) for t in buys
                if verified.get(t) is not None
                and verified[t] != before.get(t)
                and verified[t] != ("USD" if tickers[t] == "US" else "GBp")
            }
            if not surprises:
                break
            # verify_currencies persisted the corrected map; refetch converts
            # with it this time
            log.warning("unexpected quote currencies %s — recomputing", surprises)
            stage = "market data (currency retry)"
            md = data_mod.load_market_data(tickers, state.held_tickers(), cfg, today)

        prices = md.closes_gbp.ffill().iloc[-1]

        stage = "book trades"
        if trades:
            log.info("%d trade(s) signalled", len(trades))
            # booked in memory even on dry runs so the message shows the
            # post-trade portfolio; only the file write is gated below
            state = apply_trades(state, trades, today)

        state.last_run = today.isoformat()
        if not dry:
            stage = "save state"
            save_state(state, Path(cfg.portfolio_file))

        stage = "notify"
        equity_prices = prices
        if trades:
            msg = notify.format_trades(trades, state, equity_prices, table, today,
                                       cfg.starting_cash_gbp, notes)
        else:
            msg = notify.format_heartbeat(state, equity_prices, table, today,
                                          cfg.starting_cash_gbp, notes)
        if dry:
            msg = "🧪 [DRY RUN]\n" + msg
        notify.send(msg)
        if not dry:
            # stored so the /latest bot command can resend it on demand
            p = Path(cfg.last_message_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(msg)

        # Dashboard data last: the Telegram signal above is already out, so
        # a failure here alerts + reddens the run without losing the signal.
        stage = "dashboard"
        if not dry:
            benchmark = None
            try:
                b_closes, _ = data_mod.fetch_history([cfg.benchmark_ticker], cfg, "5d")
                benchmark = float(b_closes[cfg.benchmark_ticker].dropna().iloc[-1])
            except Exception:
                log.warning("benchmark fetch failed; history row will lack it")
            hist = report.append_history(
                Path(cfg.history_file), today, state.equity_gbp(prices),
                state.cash_gbp, len(state.positions), benchmark,
            )
            report.write_site_data(
                report.build_data(state, table, md.closes_gbp, hist, cfg, today,
                                  uni.names),
                Path(cfg.site_data_file),
            )

        log.info("done (dry_run=%s, trades=%d)", dry, len(trades))
        return 0

    except Exception as e:
        log.exception("run failed at stage %r", stage)
        try:
            notify.send(notify.format_error(stage, e))
        except Exception:
            log.error("error notification also failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
