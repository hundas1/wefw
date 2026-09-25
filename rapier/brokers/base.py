"""Broker interface, a dry-run wrapper and a bar-replay simulator.

Every order Rapier sends is a *bracket* identified by a deterministic prefix
(``rp-<hash>``): an entry (limit, or market when ``entry`` is None) plus a
take-profit limit and a stop-loss stop that live on the broker's servers, so a
position stays protected even if the bot or its connection dies.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("rapier.broker")
TICK = 0.25


def PrefixFor(key: str, attempt: int = 0) -> str:
    h = hashlib.sha1(key.encode()).hexdigest()[:10]
    return f"rp-{h}" if attempt == 0 else f"rp-{h}-{attempt}"


def RoundTick(px: float) -> float:
    return round(round(px / TICK) * TICK, 2)


def RoundOut(px: float, side: int) -> float:
    """Round a target *away* from the entry (up for longs, down for shorts)."""
    q = px / TICK
    return round((math.ceil(q - 1e-9) if side > 0 else math.floor(q + 1e-9)) * TICK, 2)


def TickBracket(side: int, entry: float, stop: float, r: float) -> tuple[float, float, float]:
    """Tick-rounded entry/stop/target whose target is still at least ``r`` x the rounded risk."""
    e, s = RoundTick(entry), RoundTick(stop)
    off = math.ceil(r * abs(e - s) / TICK - 1e-9) * TICK
    return e, s, round(e + side * off, 2)


class Broker(Protocol):
    name: str
    units_per_contract: int  # engine sizes in MNQ micros; NQ = 10 micros

    def Check(self) -> dict: ...
    def Position(self) -> int: ...
    def Groups(self) -> dict[str, dict]: ...  # prefix -> {"state": "working" | "active"}
    def PlaceBracket(self, prefix: str, side: int, qty: int, entry: float | None,
                      stop: float, target: float) -> None: ...
    def Cancel(self, prefix: str) -> None: ...
    def Flatten(self) -> None: ...
    def DayPnl(self) -> float | None: ...
    # optional: brokers that can report entry fills and move a working target implement
    #   FillPrice(prefix) -> float | None   and   AmendTarget(prefix, target) -> bool
    # so the executor can re-anchor a market entry's target on the real fill price


class DryRunBroker:
    """Reads real state from ``inner`` (if given) but never sends an order."""

    name = "dry-run"

    def __init__(self, inner: Broker | None = None, units_per_contract: int = 1):
        self.inner = inner
        self.units_per_contract = inner.units_per_contract if inner else units_per_contract
        self._groups: dict[str, dict] = {}
        self.log: list[tuple] = []

    def Check(self) -> dict:
        return {"dry_run": True} | (self.inner.Check() if self.inner else {})

    def Position(self) -> int:
        return self.inner.Position() if self.inner else 0

    def Groups(self) -> dict[str, dict]:
        return dict(self._groups)

    def PlaceBracket(self, prefix, side, qty, entry, stop, target):
        self._groups[prefix] = {"state": "working"}
        self.log.append(("place", prefix, side, qty, entry, stop, target))
        log.info("DRY-RUN would place %s %s x%d entry=%s stop=%s target=%s", prefix,
                 "BUY" if side > 0 else "SELL", qty, entry or "MKT", stop, target)

    def Cancel(self, prefix):
        self._groups.pop(prefix, None)
        self.log.append(("cancel", prefix))
        log.info("DRY-RUN would cancel %s", prefix)

    def Flatten(self):
        self._groups.clear()
        self.log.append(("flatten",))
        log.info("DRY-RUN would flatten")

    def DayPnl(self):
        return self.inner.DayPnl() if self.inner else None


@dataclass
class _G:
    side: int
    qty: int
    entry: float | None
    stop: float
    target: float
    state: str = "working"
    fill: float = 0.0
    fill_time: object = None


@dataclass
class SimBroker:
    """Replays bars with the backtest's conservative fill rules (for parity checks).

    Entry limits need a 1-tick trade-through; the stop wins a same-bar tie;
    on the fill bar a target only counts if the bar closes beyond it; market
    entries and flattens fill at the next bar's open. Working entries form one
    OCA group: the first fill cancels the rest.
    """

    point_value: float = 2.0
    commission_rt: float = 1.5
    slip: float = TICK
    thru: float = TICK
    units_per_contract: int = 1
    name: str = "sim"
    oca: bool = True  # False mimics Tradara: separate OTOCO groups, no cross-cancel
    _g: dict = field(default_factory=dict)
    _pos: int = 0
    _flatten: bool = False
    trades: list = field(default_factory=list)

    def Check(self):
        return {"sim": True}

    def Position(self):
        return self._pos

    def Groups(self):
        return {p: {"state": g.state} for p, g in self._g.items() if g.state in ("working", "active")}

    def PlaceBracket(self, prefix, side, qty, entry, stop, target):
        self._g[prefix] = _G(side, qty, entry, stop, target)

    def Cancel(self, prefix):
        g = self._g.get(prefix)
        if g and g.state == "working":
            g.state = "cancelled"

    def FillPrice(self, prefix):
        g = self._g.get(prefix)
        return g.fill if g and g.state == "active" else None

    def AmendTarget(self, prefix, target):
        g = self._g.get(prefix)
        if not (g and g.state == "active"):
            return False
        g.target = target
        return True

    def Flatten(self):
        self._flatten = True

    def DayPnl(self):
        return None

    def _Close(self, p, g, px, t, reason):
        pnl = g.side * (px - g.fill) * g.qty * self.point_value - self.commission_rt * g.qty
        self.trades.append(dict(prefix=p, side=g.side, qty=g.qty, entry_time=g.fill_time, entry=g.fill,
                                exit_time=t, exit=px, pnl=pnl, reason=reason))
        g.state = "done"
        self._pos -= g.side * g.qty

    def OnBar(self, t, o, h, l, c):
        if self._flatten:
            self._flatten = False
            for p, g in self._g.items():
                if g.state == "active":
                    self._Close(p, g, o - g.side * self.slip, t, "flatten")
                elif g.state == "working":
                    g.state = "cancelled"
        for p, g in list(self._g.items()):
            if g.state == "active":
                adverse, favor = (l, h) if g.side > 0 else (h, l)
                if g.side * (adverse - g.stop) <= 0:
                    gap = g.side * (o - g.stop) < 0
                    self._Close(p, g, (o if gap else g.stop) - g.side * self.slip, t, "stop")
                elif g.side * (favor - g.target) >= 0:
                    self._Close(p, g, g.target, t, "target")
        for p, g in list(self._g.items()):
            if g.state != "working" or (self.oca and self._pos != 0):
                continue
            if g.entry is None:
                g.fill = o + g.side * self.slip
            elif (g.side > 0 and l <= g.entry - self.thru) or (g.side < 0 and h >= g.entry + self.thru):
                g.fill = min(o, g.entry) if g.side > 0 else max(o, g.entry)
            else:
                continue
            g.state, g.fill_time = "active", t
            self._pos += g.side * g.qty
            if self.oca:
                for q in self._g.values():
                    if q is not g and q.state == "working":
                        q.state = "cancelled"
            adverse = l if g.side > 0 else h
            if g.side * (adverse - g.stop) <= 0:
                self._Close(p, g, g.stop - g.side * self.slip, t, "stop")
            elif g.side * (c - g.target) >= 0 and g.entry is not None:
                self._Close(p, g, g.target, t, "target")
            if self.oca:
                break


def _RootOf(symbol: str) -> str:
    s = symbol.upper().lstrip("/")
    return "MNQ" if s.startswith("MNQ") else "NQ" if s.startswith("NQ") else s


def NetPosition(items: list[dict], instrument_id: str | None, root: str) -> int:
    """Signed net quantity from a broker positions list (tolerant of field naming).

    Rows are matched by instrument id when both sides have one, otherwise by the
    exact contract root - "/MNQZ26" is never counted as NQ (or vice versa).
    """
    net = 0.0
    for p in items or []:
        pid = str(p.get("instrument_id") or "")
        sym = str(p.get("symbol") or p.get("root") or "")
        if instrument_id and pid:
            if pid != instrument_id:
                continue
        elif _RootOf(sym) != root.upper():
            continue
        for k in ("net_quantity", "quantity", "qty"):
            if p.get(k) is not None:
                try:
                    q = float(p[k])
                except (TypeError, ValueError):
                    q = 0.0
                break
        else:
            q = 0.0
        side = str(p.get("side") or p.get("position_side") or "").upper()
        net += -abs(q) if side in ("SHORT", "SELL") else abs(q) if side in ("LONG", "BUY") else q
    return int(round(net))
