# £5K Momentum Rotation — daily trade signals via Telegram

A free, self-hosted (GitHub Actions) signal system for a £5,000 trading
challenge executed **manually on Interactive Investor**. Every weekday after
US close it scores ~850 FTSE 100/250 + S&P 500 shares by cost-adjusted
momentum, checks the current portfolio, and sends one Telegram message:
either the trades to make, or a "no trades today" heartbeat with P&L.

> **This is not financial advice.** It is a rules engine you configured
> yourself. Momentum strategies have long flat/losing stretches, the
> backtest carries survivorship bias, and £5k concentrated in 5 stocks can
> draw down hard. Only run it with money you can afford to lose.

## How the strategy works

- **Score** = blended 6m/12m return (skipping the most recent month),
  divided by realised volatility, **minus** an amortised estimate of what a
  round trip in that name costs at Interactive Investor. US names carry
  ~2.6% round-trip cost (commission + 0.75% FX each way) vs ~1.6% for UK
  (commission + 0.5% stamp duty), so a US share must be meaningfully
  stronger to get picked.
- **Hold the top 5** (~£1,000 each). A name is bought only when it ranks in
  the **top 15** of the universe, but is held until it decays past **rank
  80** — this wide hysteresis band is what keeps turnover (and fees) low.
- **Buys/swaps only on Mondays**; a swap must clear a cost gate (expected
  edge over the expected ~3-month hold must exceed 2x the total switching
  cost) and never evicts a holding still in the top 15.
- **Exits fire any weekday**: close below 98% of the 200-day average, or a
  hard stop 20% below entry. Empty slots stay in cash, so the portfolio
  de-risks itself in broad downtrends.
- Max 3 of 5 positions US-listed (FX-fee and GBPUSD exposure cap).
- Expected turnover: **1–2 trades per month**.

## Setup (one-off, ~10 minutes)

1. **Create a Telegram bot**: message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts, copy the **bot token**.
2. **Get your chat id**: send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `message.chat.id` from the response.
3. **Add repo secrets**: GitHub → Settings → Secrets and variables →
   Actions → add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **Enable workflows** in the Actions tab if prompted.
5. **Test it**: Actions → *daily-signals* → Run workflow → tick *dry run*.
   You should get a Telegram message within a few minutes.

The schedule (21:45 UTC weekdays) then runs itself. GitHub disables cron
on repos with 60 days of no activity — the daily state commit keeps it
alive, but if you ever pause, re-enable from the Actions tab.

## Daily routine

- **Message says "TRADES TO MAKE"** → place those orders on Interactive
  Investor at the next opportunity (next morning is fine; the maths
  assumes roughly next-day execution). The system books them into
  `state/portfolio.json` automatically at the signal day's closing price
  with modelled costs.
- **You didn't trade, or your fill was very different** → edit
  `state/portfolio.json` on GitHub (fix `shares` / `entry_price_gbp`, or
  remove the position and add the cash back). The file is validated on
  every run, so a typo fails loudly rather than corrupting anything.
- **Message says "no trades today"** → nothing to do.
- **Message says the run FAILED** → open the Actions log; the most common
  cause is Yahoo Finance rate-limiting, which fixes itself the next day.
  The system never trades on partial data — it aborts instead.

## Backtesting

Run from the Actions tab (*backtest* workflow) or locally:

```bash
pip install -r requirements.txt
python -m backtest.backtest --years 5
python -m backtest.backtest --years 5 --sell-rank 100 --top-n 4   # param sweep
```

It replays the exact production decision code (same scoring, hysteresis
and cost model; signals computed at day t, executed at day t+1's close) and
prints CAGR, max drawdown, Sharpe, trades/month and total cost drag, plus a
buy-and-hold benchmark. **Read it with caveats**: the universe is *today's*
index members (survivorship bias inflates returns), and data is
Yahoo-adjusted daily closes. Use it to validate turnover, cost behaviour
and parameter robustness — not to extrapolate returns.

Run the unit tests any time with `python -m pytest tests/ -q`.

## Costs modelled (Interactive Investor, Core plan, Feb 2026 pricing)

| Component | UK | US |
|---|---|---|
| Commission | £3.99/trade | £3.99/trade |
| Stamp duty (buys) | 0.5% | — |
| FX fee (each way) | — | 0.75% |
| Spread/slippage assumption | 0.15% | 0.15% |
| **Round trip on £1,000** | **~1.6%** | **~2.6%** |

The £5.99/month platform fee (~1.4%/yr on £5k) is real but independent of
trading, so it is excluded from trade gating. If you change plan or II
changes pricing, update `momo/config.py`.

## Repo map

```
momo/config.py     every tunable parameter (strategy, costs, schedule)
momo/universe.py   S&P 500 + FTSE 100/250 constituents from Wikipedia (cached)
momo/data.py       yfinance batched downloads, Stooq fallback, GBp/USD -> GBP
momo/momentum.py   scoring, filters, ranking            (pure)
momo/costs.py      II cost model + swap cost gate       (pure)
momo/signals.py    hysteresis rotation engine           (pure)
momo/portfolio.py  state file schema, booking, P&L
momo/notify.py     Telegram formatting + delivery
momo/run_daily.py  the daily entry point
backtest/          event-loop backtest reusing the pure modules
state/             portfolio + universe cache (committed back by Actions)
```
