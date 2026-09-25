# Can Rapier support a living on prop-firm accounts?

Short answer: **possibly, but it's not proven yet.** The backtest says yes. The sample behind it is too
small to rely on. Below are the numbers, the risks, and a no-code-change plan to find out with real money
at small stakes.

Numbers come from `python tools/prop_income.py`. It simulates prop-account years using the backtest's own
trades. Assumptions (change them with flags):
- $2,000 trailing drawdown that stops trailing at the start balance;
- an eval to pass first ($3,000 target, $150 per attempt);
- 90% profit split, paid monthly above a $1,000 buffer;
- current risk per trade (about $200–250).
Figures are pre-tax.

## What one account makes per year (median of 1,500 simulated years)

"Haircut" = how much weaker live trading is than the backtest (share of average trade profit removed).

| live edge vs backtest | net / year | bad year (10th pct) | chance of a losing year | breaches / year |
|---|---|---|---|---|
| same as backtest | $22,700 | $17,900 | 0% | 0.00 |
| 25% weaker | $16,100 | $11,400 | 0% | 0.01 |
| **50% weaker (realistic planning case)** | **$9,500** | $5,000 | 0% | 0.06 |
| 75% weaker | $3,200 | −$150 | 15% | 0.32 |
| no edge at all | −$300 | −$600 | 75% | 1.27 |

Doubling size (`--risk 2`) roughly doubles income. At 50% weaker edge it also brings 0.66 breaches a year, so the
default size stays.

**Accounts needed for about $100k/yr** (above the US median household income, roughly $80k):
- about **5** if live matches the backtest;
- **7** at 25% weaker;
- **11** at 50% weaker;
- not reachable at 75% weaker.

Every account copies the same signals, so they win together and **breach together**. More accounts multiply income
and risk; they don't diversify it. Check how many accounts your firm allows.

## Why it isn't proven

- **Almost all the income comes from two books with short histories:**
  - `teacher-1m`: $18.5k/yr, but 34 trades over 24 days;
  - `scalp-5m`: $10.9k/yr, 28 trades over 65 days;
  - `ote-1h`: about $0.
- **Statistical check** (95% range of the true average profit per trade):
  - `scalp-5m`: +$34 to +$158. Likely real, but it's one market period.
  - `teacher-1m`: −$17 to +$117. **Not yet distinguishable from zero.**
- Backtests usually beat live trading. The FX Replay check matched outcomes 37/37, but that was the same period
  again, not new data.

## Plan to find out, with no code changes

1. **Get more history:** `rapier ibkr-backfill --start 2025-07-26` on your machine, then rerun the backtests and
   `tools/prop_income.py`. A year of real 1m data will say far more than anything else here.
2. **Run one eval account** at default size for about **60 trading days** (3 months):
   - `rapier trade --broker tradara --arm`;
   - dry run first, as in the README.
3. **Decide on the live numbers:**
   - average profit per trade at least half the backtest's (about +$35 or more), with no breach → add accounts
     gradually, about one a month;
   - average between $0 and +$35 → keep one account and keep collecting data;
   - negative after 60+ trades → stop; the edge isn't there live.
4. Keep a cash reserve for eval fees and months without payouts. Don't quit a job before step 3 says go.

## What still needs a human (the code handles everything else)

- Keep IB Gateway and the bot running. The bot reconnects after the gateway's daily restart.
- **Log in to Tradara again when its token dies.** The bot alerts on Discord and stops sending new orders;
  brackets already at the broker keep protecting open positions.
- Request payouts, and follow your firm's rule changes (the bot doesn't read them).

Handled automatically (added in the final revision):
- quarterly contract rolls, for data and orders;
- CME holidays and early closes (flat 10 minutes before);
- lost data connection: cancels resting orders, still flattens at the close;
- Yahoo outages: falls back to the cached 1h history.

Not financial advice. Past and simulated results don't guarantee future income.
