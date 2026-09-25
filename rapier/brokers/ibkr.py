"""IBKR order execution - intended for an IBKR **paper** account rehearsal.

Refuses live IBKR accounts unless ``allow_live=True`` (paper accounts start with
"DU"). Brackets use native IBKR parent/child orders; all working entry parents
share one OCA group, so the first fill cancels the other entries at IBKR.
"""

from __future__ import annotations

import logging

import pandas as pd

from .. import data as D
from .base import round_tick

log = logging.getLogger("rapier.ibkr.broker")


class IBKRBroker:
    name = "ibkr"

    def __init__(self, host="127.0.0.1", port=4002, client_id=72, root="MNQ", allow_live=False, ib=None):
        self.host, self.port, self.client_id, self.root = host, port, client_id, root
        self.units_per_contract = 1 if root == "MNQ" else 10
        self.allow_live, self.ib = allow_live, ib
        self._c = None
        self._start = None

    def _conn(self):
        if self.ib is None:
            from ib_async import IB

            self.ib = IB()
        if not self.ib.isConnected():
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)
            accts = self.ib.managedAccounts()
            if not self.allow_live and not all(a.startswith("DU") for a in accts):
                self.ib.disconnect()
                raise RuntimeError(f"IBKR accounts {accts} are not paper accounts; refusing (allow_live=False)")
        return self.ib

    def contract(self):
        if self._c is None:
            from ib_async import Future

            from ..feeds.ibkr import front_expiry

            exp = front_expiry(pd.Timestamp.now(tz=D.TZ))
            self._c = self._conn().qualifyContracts(Future(self.root, exp.strftime("%Y%m"), "CME", currency="USD"))[0]
        return self._c

    def check(self):
        ib = self._conn()
        return {"broker": "ibkr", "accounts": ib.managedAccounts(), "contract": self.contract().localSymbol,
                "position": self.position(), "rapier_open_orders": len(self._trades())}

    def _trades(self):
        return [t for t in self._conn().openTrades() if str(t.order.orderRef or "").startswith("rp-")]

    def position(self):
        cid = self.contract().conId
        return int(sum(p.position for p in self._conn().positions() if p.contract.conId == cid))

    def groups(self):
        out: dict[str, dict] = {}
        for t in self._trades():
            ref = str(t.order.orderRef)
            g = out.setdefault(ref, {"state": "active"})
            if t.order.parentId == 0 and t.orderStatus.status not in ("Filled",):
                g["state"] = "working"
        return out

    def place_bracket(self, prefix, side, qty, entry, stop, target):
        from ib_async import LimitOrder, MarketOrder, StopOrder

        ib = self._conn()
        act, rev = ("BUY", "SELL") if side > 0 else ("SELL", "BUY")
        parent = MarketOrder(act, qty) if entry is None else LimitOrder(act, qty, round_tick(entry))
        parent.orderId = ib.client.getReqId()
        parent.transmit = False
        parent.tif = "GTC" if entry is not None else "DAY"
        if entry is not None:
            parent.ocaGroup, parent.ocaType = "rapier-entries", 1
        tp = LimitOrder(rev, qty, round_tick(target), parentId=parent.orderId, transmit=False, tif="GTC")
        sl = StopOrder(rev, qty, round_tick(stop), parentId=parent.orderId, transmit=True, tif="GTC")
        for o in (parent, tp, sl):
            o.orderRef = prefix
            ib.placeOrder(self.contract(), o)

    def cancel(self, prefix):
        for t in self._trades():
            if t.order.orderRef == prefix:
                self._conn().cancelOrder(t.order)

    def flatten(self):
        from ib_async import MarketOrder

        for t in self._trades():
            self._conn().cancelOrder(t.order)
        pos = self.position()
        if pos:
            o = MarketOrder("SELL" if pos > 0 else "BUY", abs(pos))
            o.orderRef = "rp-flat"
            self._conn().placeOrder(self.contract(), o)

    def day_pnl(self):
        vals = {v.tag: v.value for v in self._conn().accountSummary() if v.currency in ("USD", "")}
        try:
            return float(vals.get("RealizedPnL", 0)) + float(vals.get("UnrealizedPnL", 0))
        except (TypeError, ValueError):
            return None
