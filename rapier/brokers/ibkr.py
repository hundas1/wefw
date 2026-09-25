"""IBKR order execution - intended for an IBKR **paper** account rehearsal.

Refuses live IBKR accounts unless ``allow_live=True`` (paper accounts start with
"DU"). Brackets use native IBKR parent/child orders; all working entry parents
share one OCA group, so the first fill cancels the other entries at IBKR.
"""

from __future__ import annotations

import logging

import pandas as pd

from .. import data as D
from .base import RoundTick

log = logging.getLogger("rapier.ibkr.broker")


class IBKRBroker:
    name = "ibkr"

    def __init__(self, host="127.0.0.1", port=4002, client_id=72, root="MNQ", allow_live=False, ib=None):
        self.host, self.port, self.client_id, self.root = host, port, client_id, root
        self.units_per_contract = 1 if root == "MNQ" else 10
        self.allow_live, self.ib = allow_live, ib
        self._c = None
        self._start = None

    def _Conn(self):
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

    def Contract(self):
        if self._c is None:
            from ib_async import Future

            from ..feeds.ibkr import FrontExpiry

            exp = FrontExpiry(pd.Timestamp.now(tz=D.TZ))
            self._c = self._Conn().qualifyContracts(Future(self.root, exp.strftime("%Y%m"), "CME", currency="USD"))[0]
        return self._c

    def Check(self):
        ib = self._Conn()
        return {"broker": "ibkr", "accounts": ib.managedAccounts(), "contract": self.Contract().localSymbol,
                "position": self.Position(), "rapier_open_orders": len(self._Trades())}

    def _Trades(self):
        return [t for t in self._Conn().openTrades() if str(t.order.orderRef or "").startswith("rp-")]

    def Position(self):
        cid = self.Contract().conId
        return int(sum(p.position for p in self._Conn().positions() if p.contract.conId == cid))

    def Groups(self):
        out: dict[str, dict] = {}
        for t in self._Trades():
            ref = str(t.order.orderRef)
            g = out.setdefault(ref, {"state": "active"})
            if t.order.parentId == 0 and t.orderStatus.status not in ("Filled",):
                g["state"] = "working"
        return out

    def PlaceBracket(self, prefix, side, qty, entry, stop, target):
        from ib_async import LimitOrder, MarketOrder, StopOrder

        ib = self._Conn()
        act, rev = ("BUY", "SELL") if side > 0 else ("SELL", "BUY")
        parent = MarketOrder(act, qty) if entry is None else LimitOrder(act, qty, RoundTick(entry))
        parent.orderId = ib.client.getReqId()
        parent.transmit = False
        parent.tif = "GTC" if entry is not None else "DAY"
        if entry is not None:
            parent.ocaGroup, parent.ocaType = "rapier-entries", 1
        tp = LimitOrder(rev, qty, RoundTick(target), parentId=parent.orderId, transmit=False, tif="GTC")
        sl = StopOrder(rev, qty, RoundTick(stop), parentId=parent.orderId, transmit=True, tif="GTC")
        for o in (parent, tp, sl):
            o.orderRef = prefix
            ib.placeOrder(self.Contract(), o)

    def Cancel(self, prefix):
        for t in self._Trades():
            if t.order.orderRef == prefix:
                self._Conn().cancelOrder(t.order)

    def _ByRef(self, prefix):
        # trades() also holds finished orders; a filled parent drops out of openTrades()
        return [t for t in self._Conn().trades() if t.order.orderRef == prefix]

    def FillPrice(self, prefix):
        for attempt in range(2):
            for t in self._ByRef(prefix):
                if t.order.parentId == 0 and t.orderStatus.status == "Filled" and t.orderStatus.avgFillPrice:
                    return float(t.orderStatus.avgFillPrice)
            if attempt == 0:
                self._Conn().sleep(0.5)  # let a just-sent market order's fill arrive
        return None

    def AmendTarget(self, prefix, target):
        for t in self._ByRef(prefix):
            if t.order.parentId != 0 and t.order.orderType == "LMT" and not t.isDone():
                t.order.lmtPrice = RoundTick(target)
                t.order.transmit = True
                self._Conn().placeOrder(self.Contract(), t.order)  # same orderId = modify
                return True
        return False

    def Flatten(self):
        from ib_async import MarketOrder

        for t in self._Trades():
            self._Conn().cancelOrder(t.order)
        pos = self.Position()
        if pos:
            o = MarketOrder("SELL" if pos > 0 else "BUY", abs(pos))
            o.orderRef = "rp-flat"
            self._Conn().placeOrder(self.Contract(), o)

    def DayPnl(self):
        vals = {v.tag: v.value for v in self._Conn().accountSummary() if v.currency in ("USD", "")}
        try:
            return float(vals.get("RealizedPnL", 0)) + float(vals.get("UnrealizedPnL", 0))
        except (TypeError, ValueError):
            return None
