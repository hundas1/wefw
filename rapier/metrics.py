"""Performance statistics and the Prop Firm Rapier goal checks."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .backtest import Result

GOALS = {
    "min_planned_rr": 1.0,     # every trade is planned at >= 1R
    "best_trade_usd": 11_000,  # at least one single trade >= $11k
    "win_rate": 0.75,
    "max_drawdown_usd": 2_000, # strictly below
}


def Summarize(res: Result) -> dict:
    t = res.trades
    eq = res.equity
    out: dict = {"trades": int(len(t))}
    if t.empty:
        return out | {"win_rate": 0.0, "net_usd": 0.0, "max_dd_usd": 0.0, "best_trade_usd": 0.0}
    wins = t.pnl > 0
    # realized-equity drawdown (closed trades) and mark-to-market drawdown
    # (running peak of close-marked equity vs the worst intrabar mark)
    real = t.sort_values("exit_time").pnl.cumsum()
    dd_closed = float((real.cummax().clip(lower=0) - real).max())
    peak = eq.equity.cummax().clip(lower=0)
    dd_mtm = float((peak - eq.equity_low).max())
    gross_w, gross_l = t.pnl[wins].sum(), -t.pnl[~wins].sum()
    days = t.groupby(pd.to_datetime(t.exit_time).dt.date).pnl.sum()
    out.update(
        wins=int(wins.sum()),
        losses=int((~wins).sum()),
        win_rate=float(wins.mean()),
        net_usd=float(t.pnl.sum()),
        avg_r=float(t.r.mean()),
        avg_win_r=float(t.r[wins].mean()) if wins.any() else 0.0,
        avg_loss_r=float(t.r[~wins].mean()) if (~wins).any() else 0.0,
        profit_factor=float(gross_w / gross_l) if gross_l > 0 else float("inf"),
        best_trade_usd=float(t.pnl.max()),
        worst_trade_usd=float(t.pnl.min()),
        min_planned_rr=float(t.planned_rr.min()),
        max_dd_usd=dd_mtm,
        max_dd_closed_usd=dd_closed,
        worst_day_usd=float(days.min()),
        best_day_usd=float(days.max()),
        max_consec_losses=int(_MaxRun(~wins.to_numpy())),
        tp1_hit_rate=float(t.tp1_hit.mean()),
    )
    return out


def GoalCheck(s: dict) -> dict[str, bool]:
    return {
        "every trade planned >= 1R": s.get("min_planned_rr", 0) >= GOALS["min_planned_rr"],
        "single trade >= $11,000": s.get("best_trade_usd", 0) >= GOALS["best_trade_usd"],
        "win rate >= 75%": s.get("win_rate", 0) >= GOALS["win_rate"],
        "max drawdown < $2,000": s.get("max_dd_usd", 1e9) < GOALS["max_drawdown_usd"],
    }


def ByBook(res: Result) -> pd.DataFrame:
    t = res.trades
    if t.empty:
        return pd.DataFrame()
    g = t.groupby("book")
    return pd.DataFrame({
        "trades": g.size(),
        "win_rate": g.pnl.apply(lambda x: (x > 0).mean()),
        "net_usd": g.pnl.sum(),
        "avg_r": g.r.mean(),
        "best_usd": g.pnl.max(),
    })


def Monthly(res: Result) -> pd.Series:
    t = res.trades
    if t.empty:
        return pd.Series(dtype=float)
    return t.groupby(pd.to_datetime(t.exit_time).dt.strftime("%Y-%m")).pnl.sum()


def _MaxRun(x: np.ndarray) -> int:
    best = cur = 0
    for v in x:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best
