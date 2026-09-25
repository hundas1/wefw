"""Estimate what the live books could pay out on prop-firm accounts, from the backtest's own trades.

Monte Carlo by trading day. Each simulated day draws a random real day independently for each
live book (teacher-1m, scalp-5m, ote-1h), then applies prop-firm rules:
  * eval: reach +eval_target before losing max_dd; each attempt costs eval_fee;
  * funded: trailing max_dd from the equity peak (end of day), which stops trailing at the start
    balance (a common prop rule); a breach means pay again and redo the eval;
  * payout: at each month end, withdraw profit above payout_buffer, times split.
--haircut 0.5 removes half of the average profit per trade (live is usually worse than backtests).
Every account copies the same signals, so N accounts pay N times as much but all breach together.

Usage: python tools/prop_income.py [--haircut 0.5] [--risk 1.0] [--accounts 5]
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from rapier import data as D
from rapier import market_hours as H

LIVE_BOOKS = {  # book -> (trades file, first day, last day) of the data it was tested on
    "teacher-1m": ("results/recent_1m_all_books_2026-08-24_to_2026-09-24/trades.csv", "2026-08-24", "2026-09-24"),
    "scalp-5m": ("results/recent_5m_all_books_2026-06-26_to_2026-09-24/trades.csv", "2026-06-26", "2026-09-24"),
    "ote-1h": (["results/oos_2024-06-15_to_2025-07-25/trades.csv", "results/main_2025-07-26_to_2026-09-24/trades.csv"],
               "2024-06-15", "2026-09-24"),
}


def TradingDays(start: str, end: str) -> pd.DatetimeIndex:
    days = pd.bdate_range(start, end)
    return days[[not H.IsClosedDay(d.date()) for d in days]]


def DailyPnl(book: str) -> list[list[float]]:
    """One entry per trading day of the test period: that day's trade P&Ls (empty list = no trade)."""
    files, start, end = LIVE_BOOKS[book]
    t = pd.concat([pd.read_csv(f) for f in ([files] if isinstance(files, str) else files)])
    t = t[t.book == book]
    day = D.TradingDay(pd.DatetimeIndex(pd.to_datetime(t.entry_time, utc=True)).tz_convert(D.TZ))
    by_day = t.pnl.groupby(day.date).apply(list).to_dict()
    return [by_day.get(d.date(), []) for d in TradingDays(start, end)]


def SimulateYear(rng, pools, a) -> dict:
    bal = peak = 0.0
    funded, fees, paid, breaches, first_pay = False, a.eval_fee, 0.0, 0, None
    for day in range(a.days):
        pnl = 0.0
        for pool, mean in pools:
            trades = pool[rng.integers(len(pool))]
            pnl += sum((x - a.haircut * mean) * a.risk for x in trades)
        bal += pnl
        floor = min(peak, 0.0) - a.max_dd if funded else -a.max_dd
        if bal <= floor:  # breached: pay again, redo the eval
            breaches += 1
            fees += a.eval_fee
            bal = peak = 0.0
            funded = False
            continue
        peak = max(peak, bal)
        if not funded and bal >= a.eval_target:
            funded, bal, peak = True, 0.0, 0.0
            fees += a.activation_fee
        if funded and (day + 1) % 21 == 0 and bal > a.payout_buffer:
            out = bal - a.payout_buffer
            paid += out * a.split
            bal -= out  # peak stays: withdrawals don't lift the drawdown floor above the start balance
            first_pay = first_pay or (day + 1) // 21
    return {"net": paid - fees, "paid": paid, "fees": fees, "breaches": breaches, "first_payout_month": first_pay}


def Main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--haircut", type=float, default=0.0, help="share of average trade profit removed (0-1)")
    p.add_argument("--risk", type=float, default=1.0, help="size multiplier vs the config's risk per trade")
    p.add_argument("--accounts", type=int, default=1)
    p.add_argument("--max-dd", type=float, default=2000.0)
    p.add_argument("--eval-target", type=float, default=3000.0)
    p.add_argument("--eval-fee", type=float, default=150.0)
    p.add_argument("--activation-fee", type=float, default=0.0)
    p.add_argument("--split", type=float, default=0.9)
    p.add_argument("--payout-buffer", type=float, default=1000.0)
    p.add_argument("--days", type=int, default=252)
    p.add_argument("--runs", type=int, default=4000)
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args(argv)
    pools = []
    for book in LIVE_BOOKS:
        pool = DailyPnl(book)
        flat = [x for d in pool for x in d]
        pools.append((pool, float(np.mean(flat)) if flat else 0.0))
        print(f"{book:11s} {len(flat):3d} trades over {len(pool):3d} days, "
              f"${sum(flat) / len(pool) * 252:,.0f}/yr at 1 account")
    rng = np.random.default_rng(a.seed)
    sims = pd.DataFrame([SimulateYear(rng, pools, a) for _ in range(a.runs)])
    net = sims.net * a.accounts
    print(f"\nhaircut {a.haircut:.0%} | risk x{a.risk} | {a.accounts} account(s) | {a.runs} simulated years")
    print(f"net per year (payouts - fees): 10th pct ${net.quantile(.1):,.0f} | median ${net.median():,.0f} | "
          f"90th pct ${net.quantile(.9):,.0f}")
    print(f"chance of losing money over the year: {(net <= 0).mean():.0%}")
    print(f"breaches per account-year: {sims.breaches.mean():.2f} | "
          f"no payout all year: {sims.first_payout_month.isna().mean():.0%} | "
          f"median first payout: month {sims.first_payout_month.median():.0f}")


if __name__ == "__main__":
    Main()
