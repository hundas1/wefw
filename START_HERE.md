# START HERE

Read this first, whether you're a person or an AI picking up the project.
It should take about 5 minutes and saves you digging through the code.

---

## 1. What this is, in one paragraph

**Prop Firm Rapier** is a trading bot for **NQ** (Nasdaq-100 futures). It looks for one kind of setup,
called **"Bias + OTE"**:
- **Bias:** the bigger trend. Price has been breaking highs, so we only buy (or the reverse, and we only sell).
- **OTE ("optimal trade entry"):** after a strong move, price pulls back about 62–79% of that move.
  The bot enters there, puts the stop just past the start of the move, and aims for at least as much profit as it risks (**≥ 1R**).

It does this on several chart timeframes at once (4 hour, 1 hour, 5 minute, 1 minute). It follows
prop-firm rules: stay under a $2,000 drawdown, be flat before the close, one trade at a time.
It's a rebuild of the owner's older bot, "Bee Sid".

## 2. Where things stand (last updated 2026-09-25)

| owner's goal | status |
|---|---|
| every trade planned at ≥ 1R | ✅ enforced in code. Live market entries now re-aim the target from the real fill price (IBKR) |
| max drawdown under $2,000 | ✅ about $700–1,000 in backtests |
| several timeframes, short ones aiming for 1R | ✅ |
| win rate ≥ 75% | ❌ whole system is 60–72%. Only the 5-minute book alone hits 75% (28 trades, small sample) |
| one single trade ≥ $11,000 | ❌ not realistic under a $2k drawdown with daily flat. See `docs/WHAT_WE_TRIED.md` |
| backtest 2025-07-26 → today | ✅ `results/main_2025-07-26_to_2026-09-24/` |
| checked on a third-party platform | ✅ FX Replay: 37 of 37 trades ended the same way (win or loss) as the backtest. See `docs/verification/` |
| can it pay a living? | ⚠️ backtest says about $22k/yr per account (about $9.5k if live is half as good); about 5–11 accounts for $100k/yr. **Not proven:** see `docs/PROP_FIRM_INCOME.md` |
| live trading | ⚠️ code is written and tested with fakes. **Never run against a real IBKR or Tradara account yet.** It needs the owner's own machine and logins |

The owner prefers **honest numbers over pretty ones**. Don't tune settings until a number looks good.
Always check on data the settings weren't tuned on.

## 3. The folders, in plain words

```
START_HERE.md          <- you are here
AGENTS.md              <- short rules for AI assistants working on this repo
README.md              <- the long version. Has a short "what was built / improved" list near the top
docs/WHAT_WE_TRIED.md  <- ideas already tested, and what happened. Read before "improving" anything
docs/PROP_FIRM_INCOME.md <- income estimate, why it's unproven, and the go/no-go plan
docs/verification/     <- evidence from replaying the trades on FX Replay

rapier/                <- THE BOT (Python package)
  data.py              get price bars (Yahoo or saved files), fix contract-roll jumps, make 5m/1h/4h bars
  indicators.py        swing highs/lows, trend direction, ATR
  strategy.py          finds the OTE setups on a chart
  features.py          extra filters (premium/discount, sweeps of yesterday's high/low, session opens)
  backtest.py          pretends to trade history bar by bar, with strict fill rules. The heart of the project
  system.py            turns the settings file into the list of "books" (one book = one timeframe's strategy)
  rapier_config.json   THE SETTINGS actually used (risk per trade, targets, filters per book)
  metrics.py           win rate, drawdown and so on, plus the goal checklist
  report.py            writes the results/ folders (summary, trade list, equity chart)
  market_hours.py      CME holidays and early closes (so the bot is flat before them)
  optimize.py          searches for better settings, with an out-of-sample guard
  live.py              asks the engine "what orders would you have right now?"
  executor.py          turns that answer into real orders on a broker, with the safety rules
  trader.py            the live loop: new 1-minute bar -> engine -> executor -> broker
  replay.py            runs the live stack over history to prove live == backtest
  cli.py               the `rapier ...` commands
  feeds/ibkr.py        price data from Interactive Brokers (read-only connection)
  brokers/base.py      what every broker must support, plus a fake broker and a simulator
  brokers/tradara.py   sends orders to Tradara (the owner's prop-firm terminal)
  brokers/ibkr.py      sends orders to an IBKR PAPER account (for rehearsal)

tests/                 <- automated checks (run `pytest`). One file per area, same names as above
tools/prop_income.py   <- "how much could this pay on prop accounts?" simulator
tools/fxreplay/        <- scripts that replay Rapier's orders on fxreplay.com in a browser
results/               <- saved backtest reports. Each folder is one run
data/                  <- price cache. NOT in git (see section 5)
```

## 4. Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ,ibkr for the Interactive Brokers parts
pytest                             # should say 47 passed
rapier fetch                       # download price bars from Yahoo into data/
rapier backtest --base 1h --start 2025-07-26 --out results/my_run   # main backtest
rapier backtest --base 1m --start 2026-08-24 --cached              # all books, needs 1m data
```

`--base` picks the clock the backtest runs on: `1h` runs the 4h+1h books, `5m` adds the 5-minute book,
and `1m` adds the 1-minute "teacher" book.

## 5. Things that will confuse you (read these)

1. **Price data isn't in git** because CME data can't be republished. `rapier fetch` gets Yahoo data, but
   Yahoo only keeps a few weeks of 1-minute bars and about 60 days of 5-minute bars. The 1m/5m results
   in `results/` used extra history taken from the old bot's saved files (`rapier import 1m file.csv`).
   The cache used here covered: 1m from 2026-08-23, 5m from 2026-06-26, 1h from 2024-05-02.
   A fresh download **won't reproduce the 1m/5m numbers exactly**. Best long-term fix:
   `rapier ibkr-backfill` (needs the owner's IBKR login).
2. **Yahoo changes old data sometimes.** On 2026-09-25 it revised September 2025 bars, and the main
   backtest moved from 20 trades / +$2,793 to 21 trades / +$3,048 with no code change.
3. **Prices are "roll-adjusted".** When the futures contract changes (quarterly), older prices are shifted
   so the chart has no jump. So Rapier's prices before 2026-09-14 are **279.25 points higher** than the
   real contract prices on FX Replay or TradingView.
4. **Sizes are in micros (MNQ).** 10 MNQ = 1 NQ. `RAPIER_ROOT=NQ` divides by 10.
5. **"Book"** means one strategy on one timeframe: `swing-4h`, `ote-1h`, `scalp-5m`, `teacher-1m`.
   `teacher-1m` is the owner's own hand-written 1-minute rules from the old bot.
6. **Naming:** functions are PascalCase (`LoadNq`, `PlaceBracket`) by the owner's choice; variables and files stay snake_case. See `AGENTS.md`.
7. **Live trading is off by default** (dry run). Real orders need BOTH `--arm` AND
   `RAPIER_I_UNDERSTAND_REAL_ORDERS=yes`, and it refuses on delayed (Yahoo) data.

## 6. What to do next (in priority order)

1. **Get real long 1-minute history.** Run `rapier ibkr-backfill --start 2025-07-26` on the owner's machine,
   then rerun the backtests with `--source ibkr`. Right now the short-timeframe books rest on 23–65 days
   of data. That's the biggest weakness.
2. **Live rehearsal**, owner's machine only: `rapier broker-check`, then several dry-run sessions,
   then an IBKR paper account with `--arm`, then one Tradara eval account. Use the 60-day go/no-go rule in
   `docs/PROP_FIRM_INCOME.md` before adding accounts.
3. **Tradara: target fix after a fill.** The code can't change an order on Tradara (no documented endpoint),
   so it uses a fixed 2-tick pad. If Tradara has a modify call, add `FillPrice()` and `AmendTarget()` to
   `rapier/brokers/tradara.py`. Copy the IBKR versions; the executor picks them up automatically.
4. **Check with the prop firm** that API/automated trading is allowed, and with IBKR that the data
   subscription allows automated ("non-display") use.

## 7. Rules that must stay true

- Never plan a trade below 1R.
- Never commit or print passwords, tokens or `.env` values (the old bot's token files were deliberately left unopened).
- Never make live trading easier to switch on by accident.
- Report results honestly, including failures.
