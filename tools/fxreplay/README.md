# FX Replay checker

These scripts check the bot on a **different platform** (fxreplay.com), to make sure the backtest isn't
fooling itself. They open FX Replay in a browser (through the `agent-browser` command-line tool) and place
the exact orders Rapier would have placed, at the same replay time.

## Files
| file | what it does |
|---|---|
| `make_schedule.py` | **Step 1.** For one day, lists every order Rapier would send (place / cancel / reprice / flatten) and the trades the backtest got |
| `run_days.py` | **Step 2.** Drives the FX Replay chart through those days and places the orders. Fast between setups, real 1x speed near each entry |
| `compare_live.py` | **Step 3.** Reads FX Replay's closed trades and lines them up with the backtest's |
| `browser.py` | Button-clicking helpers for FX Replay's web page. Fix this file if their site changes |

## How to use
1. Log in to fxreplay.com by hand in agent-browser. The scripts contain no passwords.
2. Create a Backtesting Session for asset `CME_MINI:NQ1` and open its chart.
3. Generate schedules and run them:

   ```bash
   export FXR_WORK=fxreplay_work                 # where schedules and logs go (git-ignored)
   for d in 2026-09-10 2026-09-11; do python tools/fxreplay/make_schedule.py $d; done
   python tools/fxreplay/run_days.py 2026-09-10 2026-09-11
   python tools/fxreplay/compare_live.py
   ```

## Gotchas we hit
- **Prices:** Rapier's prices are roll-adjusted. Before 2026-09-14 subtract 279.25 to get FX Replay's price; `run_days.py` does this (`ROLL`, `ADJ` at the top).
- **Holidays:** "Next Session" can land on a holiday chart (Labor Day happened once). `run_days.py` now reads the date from the "Go to a date" dialog and refuses to trade the wrong day.
- **Popups:** FX Replay's feedback popups block clicks; `browser.dismiss()` closes them.
- **Flattening:** FX Replay's "Close positions" button only closes the panel. To flatten, send an opposite market order.
- **Hidden cancels:** Rapier's simulator cancels other resting orders silently when one fills (one-cancels-all). The schedule doesn't show those cancels, so a few orders stayed live in FX Replay and filled. Treat such "extra fills" as harness noise.
- **Timing:** it's slow. A full day takes 10–30 minutes depending on how many orders there are.
