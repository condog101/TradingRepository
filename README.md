# £5K Momentum Rotation — daily trade signals via Telegram

A free, self-hosted (GitHub Actions) signal system for a £5,000 trading
challenge executed **manually on Interactive Investor**. Every weekday after
LSE close it scores ~350 FTSE 100/250 shares by cost-adjusted momentum,
checks the current portfolio, and sends one Telegram message: either the
trades to make, or a "no trades today" heartbeat with P&L.

**Why UK-only?** The account is a Stocks & Shares ISA, and HMRC rules mean
an ISA can only hold sterling cash — so every US trade would pay II's
0.75% FX fee both ways (~2.6% round trip vs ~1.6% for UK shares). In a
Trading Account or SIPP you could hold USD and cut US round trips to
~1.1%; if the money ever moves, re-add the S&P 500 to
`momo/universe.py` — the cost model and signal engine are already
multi-market.

> **This is not financial advice.** It is a rules engine you configured
> yourself. Momentum strategies have long flat/losing stretches, the
> backtest carries survivorship bias, and £5k concentrated in 5 stocks can
> draw down hard. Only run it with money you can afford to lose.

## How the strategy works

- **Score** = blended 6m/12m return (skipping the most recent month),
  divided by realised volatility, **minus** an amortised estimate of what a
  round trip in that name costs at Interactive Investor (~1.6–2% at £1,000:
  commission, 0.5% stamp duty on buys, spread).
- **Hold the top 5** (~£1,000 each). A name is bought only when it ranks in
  the **top 8** of the ~350-name universe, but is held until it decays past
  **rank 40** — this wide hysteresis band is what keeps turnover (and fees)
  low.
- **Buys/swaps only on Mondays**; a swap must clear a cost gate (expected
  edge over the expected ~3-month hold must exceed 2x the total switching
  cost) and never evicts a holding still in the top 8.
- **Exits fire any weekday**: close below 98% of the 200-day average, or a
  hard stop 20% below entry. Empty slots stay in cash, so the portfolio
  de-risks itself in broad downtrends.
- Expected turnover: **1–2 trades per month**.

## Setup (one-off, ~10 minutes)

1. **Create a Telegram bot**: message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts, copy the **bot token**.
2. **Message your bot once** (any text) — the system discovers your chat
   id automatically from that. (You can also pin it explicitly with an
   optional `TELEGRAM_CHAT_ID` secret: open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `message.chat.id`.)
3. **Add the repo secret**: GitHub → Settings → Secrets and variables →
   Actions → add `TELEGRAM_BOT_TOKEN`.
4. **Enable workflows** in the Actions tab if prompted.
5. **Test it**: Actions → *daily-signals* → Run workflow → tick *dry run*.
   You should get a Telegram message within a few minutes.

The schedule (17:45 UTC weekdays, after the 16:30 LSE close) then runs
itself. GitHub disables cron
on repos with 60 days of no activity — the daily state commit keeps it
alive, but if you ever pause, re-enable from the Actions tab.

## Dashboard

Every live run also publishes a dashboard to **GitHub Pages**:

**https://condog101.github.io/TradingRepository/**

It shows current positions (value, P&L, rank, days held, distance to stop
and 200dma, with a 13-month price chart per holding), the equity curve
rebased against the FTSE 100, drawdown and cost stats, the momentum
leaderboard (top of the ranking plus wherever your holdings sit), and the
trade log. Data refreshes with each weekday run; the history charts
accumulate from deployment day.

One-time setup:

1. **Make the repo public** (Settings → General → Danger Zone → Change
   visibility). GitHub Pages is not available on private repos on the free
   plan. ⚠️ **This makes your positions, P&L and trade history visible to
   anyone with the URL** — and the repo's code and state file too. If that
   ever becomes uncomfortable, flip the repo back to private; the daily
   signals keep working (the deploy step just starts skipping, by design).
2. Run the *daily-signals* workflow once (not dry-run) — the workflow
   enables Pages itself on first deploy.

## Bot commands

Send **/latest** (or `/status`) to the bot at any time to get the most
recent daily update resent. There is no always-on server — a scheduled
workflow (`bot.yml`) polls for commands every ~10 minutes, so expect the
reply within 10–15 minutes, not instantly. Only your own chat gets
replies. Disable the *bot-commands* workflow from the Actions tab if you
don't use this.

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
buy-and-hold benchmark. Tick the *sweep* input to grid the swap-gate
parameters instead of a single run. **Read it with caveats**: the universe
is *today's* index members (survivorship bias inflates returns), and data
is Yahoo-adjusted daily closes. Use it to validate turnover, cost
behaviour and parameter robustness — not to extrapolate returns.

### Recorded results (Oct 2022 – Jul 2026, 3.8y, survivorship-biased)

The Jul 2026 sweep showed turnover was driven by the weekly swap, not
rank-decay exits, and that requiring a holding to fall past **rank 20**
before it can be swapped improved every metric at once. Defaults were set
accordingly (`swap_out_rank=20`, `swap_safety_factor=3.0`):

| Config | CAGR | Max DD | Sharpe | Trades/mo | Cost drag |
|---|---|---|---|---|---|
| Original (swap floor 8, factor 2) | +17.8% | -30.5% | 0.82 | 4.5 | 11.9%/yr |
| **Adopted (floor 20, factor 3)** | **+25.8%** | **-25.3%** | **1.13** | 3.3 | 9.2%/yr |
| FTSE 100 buy & hold | +16.2% | — | — | 0 | 0 |

Neighbouring cells (floor 20 with factors 2–4) scored +23.8% to +32.5%,
so the adopted point sits on a plateau, not a spike. Residual turnover
(~3/month) comes from 200dma trend exits and re-entries, which is the
strategy working as designed, not churn.

Run the unit tests any time with `python -m pytest tests/ -q`.

## Costs modelled (Interactive Investor, Core plan, Feb 2026 pricing)

| Component | UK shares |
|---|---|
| Commission | £3.99/trade |
| Stamp duty (buys) | 0.5% |
| Spread/slippage assumption | 0.25% (FTSE 250-honest; FTSE 100 is tighter) |
| **Round trip on £1,000** | **~1.8%** |

For reference: US shares in this ISA would cost ~2.6% per round trip
(0.75% FX each way on top of commission), which is why they're excluded.
The £5.99/month platform fee (~1.4%/yr on £5k) is real but independent of
trading, so it is excluded from trade gating. If you change plan or II
changes pricing, update `momo/config.py`.

## Repo map

```
momo/config.py     every tunable parameter (strategy, costs, schedule)
momo/universe.py   FTSE 100/250 constituents from Wikipedia (cached)
momo/data.py       yfinance batched downloads, Stooq fallback, GBp -> GBP
momo/momentum.py   scoring, filters, ranking            (pure)
momo/costs.py      II cost model + swap cost gate       (pure)
momo/signals.py    hysteresis rotation engine           (pure)
momo/portfolio.py  state file schema, booking, P&L
momo/notify.py     Telegram formatting + delivery
momo/run_daily.py  the daily entry point
backtest/          event-loop backtest reusing the pure modules
state/             portfolio + universe cache (committed back by Actions)
```
