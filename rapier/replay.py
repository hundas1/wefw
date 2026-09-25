"""Bar-by-bar replay of the live stack (engine -> executor -> simulated broker).

This is the end-to-end check that live execution reproduces the backtest: at
each completed 1m bar the engine only sees data up to that bar (exactly as the
live loop does), the executor sends orders, and :class:`SimBroker` fills them
on the *following* bars.
"""

from __future__ import annotations

import copy

import pandas as pd

from . import data as D
from .backtest import Market, run
from .brokers.base import SimBroker
from .executor import ExecConfig, Executor
from .live import engine_view
from .system import build


def live_params(params: dict, books: tuple[str, ...]) -> dict:
    p = copy.deepcopy(params)
    p["swing"]["enabled"] = False
    p["intraday"]["enabled"] = "ote-1h" in books
    p["scalp"]["enabled"] = any(b.startswith("scalp") for b in books)
    p["teacher"]["enabled"] = "teacher-1m" in books
    return p


def replay(frames: dict[str, pd.DataFrame], params: dict, books: tuple[str, ...], start, end,
           window: tuple[float, float] = (8.0, 16.75), m1_days: int = 4, h1_days: int = 300):
    p = live_params(params, books)
    sim = SimBroker(slip=0.25)
    ex = Executor(sim, ExecConfig(live_books=books, stale_after=pd.Timedelta("10min")))
    m1 = frames["1m"]
    idx = m1.index[(m1.index >= pd.Timestamp(start, tz=D.TZ)) & (m1.index < pd.Timestamp(end, tz=D.TZ))]
    for t in idx:
        row = m1.loc[t]
        sim.on_bar(t, row.open, row.high, row.low, row.close)
        hour = t.hour + t.minute / 60
        if not (window[0] <= hour < window[1]) and not sim.position() and not sim.groups():
            continue
        cut = t + pd.Timedelta("1min")
        sl = {k: f[(f.index < cut) & (f.index >= cut - pd.Timedelta(days=m1_days if k != "1h" else h1_days))]
              for k, f in frames.items()}
        mkt = Market.from_1h(sl["1h"], {k: sl[k] for k in ("1m", "5m", "15m")}, base_tf="1m")
        view, _ = engine_view(mkt, p, lookback_days=m1_days - 1)
        ex.step(view, wall=cut + pd.Timedelta(seconds=3))
    return pd.DataFrame(sim.trades)


def backtest_trades(frames, params, books, start, end) -> pd.DataFrame:
    p = live_params(params, books)
    mkt = Market.from_1h(frames["1h"], {k: frames[k] for k in ("1m", "5m", "15m")}, base_tf="1m")
    b, r = build(p)
    return run(mkt, b, r, start=start, end=end).trades


def compare(bt: pd.DataFrame, live: pd.DataFrame, tol: pd.Timedelta = pd.Timedelta("2min")) -> dict:
    """Pair trades by side and entry time (live entries may be one bar later)."""
    used, pairs = set(), []
    for i, t in bt.iterrows():
        et = pd.Timestamp(t.entry_time)
        for j, u in live.iterrows():
            if j in used or u.side != t.side:
                continue
            if abs(pd.Timestamp(u.entry_time) - et) <= tol:
                used.add(j)
                pairs.append((i, j))
                break
    mb = {i for i, _ in pairs}
    same_result = sum((bt.loc[i].pnl > 0) == (live.loc[j].pnl > 0) for i, j in pairs)
    return {"backtest_trades": len(bt), "live_trades": len(live), "matched": len(pairs),
            "same_win_loss": int(same_result), "backtest_only": sorted(set(bt.index) - mb),
            "live_only": sorted(set(live.index) - used),
            "backtest_pnl": float(bt.pnl.sum()) if len(bt) else 0.0,
            "live_pnl": float(live.pnl.sum()) if len(live) else 0.0}
