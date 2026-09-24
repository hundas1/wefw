"""Live / paper signal loop.

Every cycle re-fetches bars, re-runs the exact backtest engine up to "now" and
diffs the result against the saved state, so live signals can never drift
from what the backtest would have done. It emits:

* ``LIMIT``  - an armed OTE limit order (entry / stop / target) worth resting now
* ``CANCEL`` - a previously armed limit that is no longer valid
* ``ENTRY`` / ``EXIT`` - paper fills and exits

Orders go to :class:`PaperBroker` (a JSONL journal). Routing real orders to a
broker (Tradovate / Rithmic / IBKR) needs your own credentials and adapter;
implement :class:`Broker` for that.
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
from .backtest import Market, _asof, _context_ok, run
from .system import build, load_params

STATE = D.DATA_DIR / "live_state.json"
JOURNAL = D.DATA_DIR / "paper_journal.jsonl"


class Broker(Protocol):
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
    scalp = params["scalp"].get("enabled")
    lower = tuple(params["scalp"]["tfs"]) if scalp else ()
    d = D.load_nq(("1h",) + lower, refresh=refresh)
    frames = {tf: d[tf] for tf in lower}
    return Market.from_1h(d["1h"], frames, base_tf=min(lower, key=lambda x: int(x[:-1])) if lower else "1h")


def armed_orders(mkt: Market, params: dict, consumed: frozenset | None = None) -> list[dict]:
    """Limits the engine would currently have resting, after bias/filter checks.

    ``consumed`` comes from a backtest run over recent history, so a limit is
    dropped exactly when the engine would have treated it as traded into.
    """
    books, risk = build(params)
    base = mkt.base
    if consumed is None:
        start = (base.index[-1] - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
        consumed = run(mkt, books, risk, start=start, close_at_end=False).consumed
    step = base.index.to_series().diff().mode().iloc[0]
    # "now" is the end of the last completed base bar, exactly what the backtest sees
    now_ns = np.array([(base.index[-1] + step).value])
    feat = mkt.context().iloc[-1]
    out = []
    for b in books:
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
            key = f"{b.name}:{side}:{int(S['id'][k])}"
            if key in consumed:
                continue
            arr = lambda v: np.array([v])
            if not _context_ok(b, side, 0, arr(feat.pd_pos), arr(feat.vs_midnight), arr(feat.vs_dopen),
                               arr(bool(feat.swept_pdl)), arr(bool(feat.swept_pdh))):
                continue
            risk_pts = side * (E - S["S"][k])
            qty = min(risk.max_contracts, int(b.risk_usd // (risk_pts * risk.point_value))) if risk_pts > 0 else 0
            out.append({"type": "LIMIT", "book": b.name, "side": "LONG" if side > 0 else "SHORT",
                        "entry": round(E * 4) / 4, "stop": round(S["S"][k] * 4) / 4,
                        "tp1": round((E + side * b.tp1_r * risk_pts) * 4) / 4, "mnq": qty,
                        "confirm": b.confirm, "key": f"{b.name}:{side}:{int(S['id'][k])}"})
    return out


def cycle(params: dict, broker: Broker, webhook: str | None, refresh: bool = True) -> list[dict]:
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
    orders = {o["key"]: o for o in armed_orders(mkt, params, res.consumed)}
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
