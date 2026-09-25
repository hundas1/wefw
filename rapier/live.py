"""Live / paper signal loop.

Every cycle re-fetches bars, re-runs the exact backtest engine up to "now" and
diffs the result against the saved state, so live signals can never drift
from what the backtest would have done. It emits:

* ``LIMIT``  - an armed OTE limit order (entry / stop / target) worth resting now
* ``CANCEL`` - a previously armed limit that is no longer valid
* ``ENTRY`` / ``EXIT`` - paper fills and exits

Events go to :class:`PaperBroker` (a JSONL journal). Real order routing lives
in :mod:`rapier.trader` / :mod:`rapier.executor`, which reuse :func:`engine_view`.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from . import data as D
from .backtest import TF_DELTA, Market, _asof, _context_ok, run
from .brokers.base import tick_bracket
from .indicators import ema
from .system import build, load_params

STATE = D.DATA_DIR / "live_state.json"
JOURNAL = D.DATA_DIR / "paper_journal.jsonl"


class EventSink(Protocol):
    def send(self, event: dict) -> None: ...


class PaperBroker:
    def __init__(self, path: Path = JOURNAL):
        self.path = path

    def send(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")


def notify(event: dict, webhook: str | None) -> None:
    line = " | ".join(f"{k}={v}" for k, v in event.items())
    print(line, flush=True)
    if webhook:
        body = json.dumps({"content": f"**Prop Firm Rapier** {line}"[:1900]}).encode()
        req = urllib.request.Request(webhook, body, {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # never let a notification failure stop the loop
            print(f"webhook error: {e}", flush=True)


def build_market(params: dict, refresh: bool = True) -> Market:
    lower = tuple(params["scalp"]["tfs"]) if params["scalp"].get("enabled") else ()
    if params.get("teacher", {}).get("enabled"):
        lower = tuple(dict.fromkeys(lower + (params["teacher"]["tf"],)))
    d = D.load_nq(("1h",) + lower, refresh=refresh)
    frames = {tf: d[tf] for tf in lower}
    return Market.from_1h(d["1h"], frames, base_tf=min(lower, key=lambda x: int(x[:-1])) if lower else "1h")


def armed_orders(mkt: Market, params: dict, res=None) -> list[dict]:
    """Limits the engine would fill if price trades through them during the *next* bar.

    Every entry gate the backtest applies at the fill bar is checked here for that
    bar: bias, filters, anchor not consumed and not too old, session / flat time,
    per-day trade cap, stop-size range, size >= 1, the daily loss stop and the
    16:00-18:00 no-entry window. ``res`` is the engine run over recent history
    (consumed anchors, today's trades, drawdown throttle); computed if omitted.
    """
    books, risk = build(params)
    base = mkt.base
    if res is None:
        start = (base.index[-1] - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
        res = run(mkt, books, risk, start=start, close_at_end=False)
    consumed, risk_scale = res.consumed, res.risk_scale
    step = base.index.to_series().diff().mode().iloc[0]
    # "now" is the end of the last completed base bar = start of the bar the order would fill in
    now = base.index[-1] + step
    now_ns = np.array([now.value])
    hour = now.hour + now.minute / 60
    today = D.trading_day(pd.DatetimeIndex([now]))[0]
    entered, day_pnl = {}, 0.0
    t = res.trades
    if len(t):
        et = pd.DatetimeIndex(pd.to_datetime(t.entry_time))
        entered = t[D.trading_day(et) == today].groupby("book").size().to_dict()
        closed = t[t.exit_time.notna()]
        if len(closed):
            xt = pd.DatetimeIndex(pd.to_datetime(closed.exit_time))
            day_pnl = float(closed.pnl[D.trading_day(xt) == today].sum())
    if day_pnl <= -risk.daily_loss_limit or (risk.flatten_eod and (16.0 <= hour < 18.0 or hour >= risk.flatten_time and hour < 17.5)):
        return []
    open_books = {tr.book for tr in res.open_trades}
    if len(open_books) >= risk.max_open:
        return []
    feat = mkt.context().iloc[-1]
    out = []
    for b in books:
        if b.name in open_books or entered.get(b.name, 0) >= b.max_trades_day:
            continue
        if b.sessions and not any(a <= hour < z for a, z in b.sessions):
            continue
        if b.flat_after is not None and b.flat_after <= hour < 17.5:
            continue
        s = mkt.setups(b.tf, b.setup)
        k = int(_asof(s.close_time, now_ns)[0])
        if k < 0:
            continue
        bias = 2
        for btf in b.bias_tfs:
            ct, tr = mkt.trend(btf, risk.bias_k)
            v = int(tr[_asof(ct, now_ns)[0]])
            bias = v if bias == 2 else (bias if bias == v else 0)
        for side, S in ((1, s.long), (-1, s.short)):
            E = S["E"][k]
            if math.isnan(E) or not (bias == 2 or bias == side) or k - int(S["id"][k]) > b.max_age:
                continue
            key = f"{b.name}:{side}:{s.index[int(S['id'][k])].isoformat()}"
            if key in consumed:
                continue
            arr = lambda v: np.array([v])
            if not _context_ok(b, side, 0, arr(feat.pd_pos), arr(feat.vs_midnight), arr(feat.vs_dopen),
                               arr(bool(feat.swept_pdl)), arr(bool(feat.swept_pdh))):
                continue
            if b.ema_tf:
                ef = mkt.frames[b.ema_tf]
                ei = int(_asof((ef.index + TF_DELTA[b.ema_tf]).asi8, now_ns)[0])
                if ei < 0 or abs(E - ema(ef["close"].to_numpy()[:ei + 1], 9)[-1]) > b.ema_dist:
                    continue
            stop = E - side * b.fixed_stop_pts if b.fixed_stop_pts else S["S"][k]
            risk_pts = side * (E - stop)
            if not (b.min_stop_pts <= risk_pts <= b.max_stop_pts):
                continue
            budget = b.risk_usd * risk_scale
            qty = min(risk.max_contracts, int(budget // (risk_pts * risk.point_value)))
            if qty < 1:
                continue
            e_r, s_r, t_r = tick_bracket(side, E, stop, b.tp1_r)
            out.append({"type": "LIMIT", "book": b.name, "side": "LONG" if side > 0 else "SHORT",
                        "entry": e_r, "stop": s_r, "tp1": t_r, "mnq": qty, "confirm": b.confirm, "key": key})
    return out


def engine_view(mkt: Market, params: dict, lookback_days: int = 20):
    """Run the engine up to the last completed bar and describe its desired state."""
    from .executor import View

    books, risk = build(params)
    base = mkt.base
    start = (base.index[-1] - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    res = run(mkt, books, risk, start=start, close_at_end=False)
    step = base.index.to_series().diff().mode().iloc[0]
    last = base.index[-1]
    t = res.trades
    forced, day_pnl = [], 0.0
    if len(t):
        closed = t[t.exit_time.notna()]
        now_day = D.trading_day(pd.DatetimeIndex([last]))[0]
        exits = pd.DatetimeIndex(pd.to_datetime(closed.exit_time))
        day_pnl = float(closed.pnl[(D.trading_day(exits) == now_day)].sum()) if len(closed) else 0.0
        last_exits = closed[(exits == last) & closed.exit_reason.isin(["eod", "flat", "time"])]
        forced = last_exits[["book", "exit_reason"]].rename(columns={"exit_reason": "reason"}).to_dict("records")
    view = View(now=last + step, last_bar=last, last_close=float(base.close.iloc[-1]),
                armed=armed_orders(mkt, params, res), open_trades=list(res.open_trades),
                forced_exits=forced, engine_day_pnl=day_pnl,
                confirm_books=frozenset(b.name for b in books if b.confirm),
                book_flat={b.name: b.flat_after for b in books if b.flat_after is not None})
    return view, res


def cycle(params: dict, broker: EventSink, webhook: str | None, refresh: bool = True) -> list[dict]:
    mkt = build_market(params, refresh=refresh)
    books, risk = build(params)
    start = mkt.base.index[-1] - pd.Timedelta(days=20)
    res = run(mkt, books, risk, start=start.strftime("%Y-%m-%d"), close_at_end=False)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"trades": {}, "orders": {}}
    events = []
    for _, t in res.trades.iterrows():
        key = f"{t.book}:{t.entry_time}"
        prev = state["trades"].get(key)
        if prev is None:
            events.append({"type": "ENTRY", "book": t.book, "side": "LONG" if t.side > 0 else "SHORT",
                           "time": t.entry_time, "entry": t.entry, "stop": t.stop, "tp1": t.tp1,
                           "mnq": t.qty})
        if pd.notna(t.exit_time) and (prev is None or not prev.get("closed")):
            events.append({"type": "EXIT", "book": t.book, "time": t.exit_time, "price": t.exit_price,
                           "pnl": round(t.pnl, 2), "reason": t.exit_reason})
        state["trades"][key] = {"closed": bool(pd.notna(t.exit_time))}
    orders = {o["key"]: o for o in armed_orders(mkt, params, res)}
    for key, o in orders.items():
        if key not in state["orders"]:
            events.append(o)
    for key in set(state["orders"]) - set(orders):
        events.append({"type": "CANCEL", "key": key})
    state["orders"] = orders
    # On the very first run, only announce what is live right now.
    first = not STATE.exists()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, default=str, indent=1))
    if first:
        events = [e for e in events if e["type"] == "LIMIT"]
    for e in events:
        broker.send(e)
        notify(e, webhook)
    return events


def loop(interval: int = 300, once: bool = False, config: str | None = None) -> None:
    params = load_params(config)
    webhook = os.environ.get("RAPIER_DISCORD_WEBHOOK")
    broker = PaperBroker()
    while True:
        try:
            ev = cycle(params, broker, webhook)
            print(f"[{pd.Timestamp.now(tz=D.TZ):%Y-%m-%d %H:%M}] cycle ok, {len(ev)} event(s)", flush=True)
        except Exception as e:
            print(f"cycle failed: {e!r}", flush=True)
        if once:
            return
        time.sleep(interval)
