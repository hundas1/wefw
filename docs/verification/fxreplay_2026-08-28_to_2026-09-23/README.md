# FX Replay check, 2026-08-28 → 2026-09-23

**What:** Rapier's orders were placed by script on fxreplay.com (NQ, "Light" slippage, $100k practice
balance), at the same replay times as the backtest. The replay played at real 1x speed around every entry.

**File:** `trades_rapier_vs_fxreplay.csv`. There's one row per trade. `same_win_or_loss` says whether both
agreed. Rows marked `EXTRA FILL` happened only in FX Replay; the `note` column says why (all were script
problems, not strategy).

**Result:**
- 37 of 37 backtest trades filled on FX Replay, and all 37 ended the same way (24 wins each).
- Net P&L: backtest +$1,928, FX Replay +$2,248.
  - FX Replay charged no commission in this session.
  - One row (Aug 28 11:26) is inflated by a script bug.

**Note:** the backtest column is from the code *before* the "market entries fill at next open" change
that this check led to. That's why a few of its numbers differ slightly from today's `results/` folder.
