# What we already tried, and what happened

A log so the next person doesn't repeat work. Newest first. Numbers are from backtests unless noted.

## 2026-09-25: third-party check on FX Replay (18 trading days, 2026-08-28 → 09-23)
- Replayed Rapier's exact orders on fxreplay.com (`tools/fxreplay/`). **37 of 37 trades ended the same way (win or loss)** as the backtest.
- **Found:** market-order entries filled −8 to +17 ticks away from the backtest's price, so a few trades were really 0.87–0.99R, not 1R.
- **Fixed:**
  - the backtest now fills market entries at the next bar's open;
  - the live bot moves the target after the fill (IBKR);
  - Tradara gets a fixed 2-tick pad.
- **Cost:** the 5m run went from +$2,710 to +$2,551, the 1m run from +$2,503 to +$2,454. No wins turned into losses.
- **Considered, not done:** an extra tick of cushion on limit-order targets. Real exchange limits can't fill worse than their price; only FX Replay's slippage setting did that.
- **Tradara has no one-cancels-all across separate orders.** Measured in the simulator: no double fills in that month. It's still a theoretical risk.

## Live-stack replay (engine → executor → simulated broker vs the plain backtest)
- First version reproduced 43 of 44 trades but added **52 extra trades**, because orders ignored the session and daily limits. Fixed.
- Then found and fixed:
  - orders not re-priced when the OTE level moved;
  - signal names changing as the data window slid (caused endless cancel/replace);
  - tick rounding leaving targets 0.25 short of 1R.
- Now: 42 of 43 match with the same win/loss.

## Chasing 75% win rate and a single $11k trade
- Searched 4,600+ settings combinations, always scoring on the worse of in-sample and out-of-sample.
- Settings that showed 75–80% on one half of the data dropped to 20–40% on the other half: curve-fitting.
- The only config with an $11.6k trade lost $5.7k with a $10.2k drawdown on the previous year. Rejected.
- The math behind this:
  - 75% win rate at ≥ 1R means profit factor ≥ 3, while raw OTE touches win about 42–50%;
  - the best filters robustly reach about 55–66%;
  - an $11k trade with ~$400 risk needs roughly 27R, but the biggest move any OTE trade made in 14 months (flat daily) was 13.8R.

## Owner's 1-minute rules (`teacher-1m`)
- Breakeven at 0.5R, as the old bot had it: 25% / 44% win rate on the two halves, because it scratched trades that later won. Removed.
- A fixed 28.75-pt stop was worse than a stop beyond the start of the move. Changed.
- 9EMA "golden belt" confluence on 1m left almost no trades, and those lost. Off.

## Old "Bee Sid" bot's claims (checked, not trusted)
- Its "75% win rate" left out 32 breakeven trades out of 60. Counting them it was 35%, and the filters were picked on the same data.
- Its "1m" data before 2026-08-23 was really 5m/2m bars copied onto a 1-minute grid. Rapier refuses to import such days.
- The owner's real trading journal: 46% win rate, +$28.5k, best trade $4,725, max drawdown $2,575. That's the honest benchmark.

## Data problems
- Yahoo `NQ=F` jumps at every contract roll. Rapier detects and back-adjusts these.
- Yahoo revises old bars now and then, so results can shift with no code change.
