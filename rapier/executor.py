"""Mirror the engine's desired state onto a broker, with hard safety rails.

Each cycle (after every completed bar) the engine says what it would have:
resting OTE limits, an open position, or a forced exit. The executor diffs that
against the broker and sends only the difference:

* limit books: resting entry brackets are placed / cancelled to match the
  engine's armed LIMIT list;
* confirmation books: when the engine enters at a bar close, a market bracket
  is sent immediately;
* one position at a time (the backtest's ``max_open=1``): as soon as the
  broker shows a position, every other working entry is cancelled;
* engine exits the broker cannot know about (end-of-day / book flat time) are
  sent as a flatten; stops and targets themselves live at the broker.

Safety rails (any one blocks new orders): not armed (dry-run), stale data,
market closed / after the flatten time, daily loss limit hit (kill switch:
cancel everything, flatten, stay off until the next trading day), order
sanity (price near market, stop/target on the right side, size caps), and a
per-book allowlist - only bracket-managed books are allowed live.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import data as D
from . import market_hours as H
from .brokers.base import Broker, PrefixFor, RoundOut, RoundTick

log = logging.getLogger("rapier.exec")

# Books whose whole management is entry + fixed stop + fixed target (a broker
# bracket reproduces them exactly). swing-4h needs partial exits, a
# break-even move and a trailing stop, so it stays signal-only.
BRACKET_BOOKS = ("teacher-1m", "scalp-5m", "scalp-15m", "ote-1h")


@dataclass
class View:
    now: pd.Timestamp            # end of the last completed bar
    last_bar: pd.Timestamp       # start of the last completed bar
    last_close: float
    armed: list[dict]            # LIMIT dicts from live.armed_orders
    open_trades: list            # backtest Trade objects still open
    forced_exits: list[dict]     # engine exits on the last bar the broker can't see (eod / flat / time)
    engine_day_pnl: float = 0.0
    confirm_books: frozenset = frozenset()
    book_flat: dict = field(default_factory=dict)  # book -> ET hour it must be flat by


@dataclass
class ExecConfig:
    live_books: tuple[str, ...] = ("teacher-1m", "scalp-5m", "ote-1h")
    max_qty: int = 10                  # per order, in broker contracts
    daily_loss_limit: float = 600.0
    stale_after: pd.Timedelta = pd.Timedelta("3min")
    flatten_time: float = 16.67        # ET hours
    max_entry_dev: float = 0.03        # entry within 3% of the last price
    max_stop_pts: float = 400.0
    # market entries can fill worse than the engine's price (FX Replay filled 2 ticks off);
    # widen their targets so reward >= risk still holds for up to this much slippage
    market_slip_ticks: int = 2
    early_close_buffer: float = 10 / 60  # flatten this long (hours) before a holiday early close

    def __post_init__(self):
        bad = set(self.live_books) - set(BRACKET_BOOKS)
        if bad:
            raise ValueError(f"books {sorted(bad)} need order modification and cannot run live yet")


def MarketOpen(t: pd.Timestamp) -> bool:
    t = t.tz_convert(D.TZ)
    h = t.hour + t.minute / 60
    if t.dayofweek == 5 or (t.dayofweek == 4 and h >= 17) or (t.dayofweek == 6 and h < 18):
        return False
    if H.IsClosedDay(t.date()) and h < 18:
        return False
    return not (17 <= h < 18)


@dataclass
class Executor:
    broker: Broker
    cfg: ExecConfig = field(default_factory=ExecConfig)
    state_path: Path | None = None
    state: dict = field(default_factory=lambda: {"orders": {}, "killed_day": None, "noted": []})

    def __post_init__(self):
        if self.state_path and Path(self.state_path).exists():
            self.state = json.loads(Path(self.state_path).read_text())

    def _Save(self):
        if self.state_path:
            Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.state_path).write_text(json.dumps(self.state, default=str, indent=1))

    def _Note(self, msg: str, out: list, once_key: str | None = None) -> None:
        if once_key:
            if once_key in self.state["noted"]:
                return
            self.state["noted"] = (self.state["noted"] + [once_key])[-500:]
        out.append(msg)
        log.info(msg)

    # ---------------------------------------------------------------- helpers
    def _Qty(self, mnq: int) -> int:
        return min(self.cfg.max_qty, int(mnq) // self.broker.units_per_contract)

    def _Sane(self, side: int, entry: float, stop: float, target: float, last: float) -> str | None:
        if abs(entry - last) > self.cfg.max_entry_dev * last:
            return f"entry {entry} too far from last price {last}"
        if side * (entry - stop) <= 0 or side * (target - entry) <= 0:
            return "stop/target on the wrong side"
        if abs(entry - stop) > self.cfg.max_stop_pts:
            return f"stop {abs(entry - stop):.1f} pts exceeds cap"
        if side * (target - entry) < abs(entry - stop) - 1e-6:
            return "target below 1R"
        return None

    def _Place(self, key: str, book: str, side: int, mnq: int, entry, stop, target, last, out,
               ref_entry: float | None = None, r_mult: float | None = None) -> bool:
        """``entry=None`` sends a market entry; ``ref_entry`` is the engine's price for it and
        ``r_mult`` its planned reward:risk, re-applied to the real fill where the broker allows."""
        qty = self._Qty(mnq)
        if qty < 1:
            self._Note(f"skip {key}: size {mnq} MNQ is below one {self.broker.name} contract", out, f"small:{key}")
            return False
        why = self._Sane(side, entry if entry is not None else (ref_entry or last), stop, target, last)
        if why:
            self._Note(f"REFUSED {key}: {why}", out, f"refused:{key}")
            return False
        rec = self.state["orders"].get(key, {"attempt": -1})
        prefix = PrefixFor(key, rec["attempt"] + 1)
        self.broker.PlaceBracket(prefix, side, qty, None if entry is None else RoundTick(entry),
                                  RoundTick(stop), RoundTick(target))
        self.state["orders"][key] = {"attempt": rec["attempt"] + 1, "prefix": prefix, "book": book,
                                     "status": "sent", "side": side, "qty": qty,
                                     "levels": [None if entry is None else RoundTick(entry),
                                                RoundTick(stop), RoundTick(target)],
                                     "r_mult": r_mult}
        self._Note(f"PLACE {book} {'BUY' if side > 0 else 'SELL'} x{qty} "
                   f"entry={'MKT' if entry is None else RoundTick(entry)} stop={RoundTick(stop)} "
                   f"target={RoundTick(target)} [{prefix}]", out)
        return True

    def _ReanchorTargets(self, out: list) -> None:
        """Market entries fill away from the engine's price (FX Replay: -8..+17 ticks), so move
        each filled market bracket's target to the planned R measured from the actual fill."""
        fill_price = getattr(self.broker, "FillPrice", None)
        amend = getattr(self.broker, "AmendTarget", None)
        if not (fill_price and amend):
            return  # e.g. Tradara: no documented amend; the static slippage pad stays
        active = {p for p, g in self.broker.Groups().items() if g["state"] == "active"}
        for rec in self.state["orders"].values():
            p = rec.get("prefix")
            if rec.get("r_mult") is None or rec.get("reanchored") or p not in active:
                continue
            fill = fill_price(p)
            if fill is None:
                continue
            rec["reanchored"] = True
            side, stop, old = rec["side"], rec["levels"][1], rec["levels"][2]
            if side * (fill - stop) <= 0:
                continue  # filled through the stop; the stop order handles it
            new = RoundOut(fill + side * rec["r_mult"] * abs(fill - stop), side)
            if new != old and amend(p, new):
                rec["levels"][2] = new
                self._Note(f"AMEND {p}: filled {fill}, target {old} -> {new} "
                           f"({rec['r_mult']:.2f}R from the actual fill)", out)

    def Blind(self, wall: pd.Timestamp | None = None) -> list[str]:
        """No market data this cycle: pull working entries, and still flatten at the day's flat time."""
        out: list[str] = []
        wall = wall or pd.Timestamp.now(tz=D.TZ)
        if not MarketOpen(wall):
            return out
        groups = self.broker.Groups()
        working = [p for p, g in groups.items() if g["state"] == "working"]
        by_prefix = {rec["prefix"]: rec for rec in self.state["orders"].values() if "prefix" in rec}
        for p in working:
            self.broker.Cancel(p)
            if p in by_prefix:
                by_prefix[p]["status"] = "cancelled"  # so it is re-placed once data is back
        if working:
            self._Note(f"NO DATA: cancelled {len(working)} working entries", out)
        et = wall.tz_convert(D.TZ)
        hour = et.hour + et.minute / 60
        early = H.EarlyClose(et.date())
        flat_at = min(self.cfg.flatten_time, early - self.cfg.early_close_buffer) if early else self.cfg.flatten_time
        pos = self.broker.Position()
        if pos and flat_at <= hour < 18:
            self.broker.Flatten()
            self._Note(f"FLATTEN position {pos} (end of day, no data)", out)
        self._Save()
        return out

    def _Kill(self, reason: str, day: str, out: list) -> None:
        self.broker.Flatten()
        self.state["killed_day"] = day
        self._Note(f"KILL SWITCH: {reason}. Cancelled Rapier orders, flattened, off until next trading day.", out)

    # ------------------------------------------------------------------- step
    def Step(self, v: View, wall: pd.Timestamp | None = None) -> list[str]:
        out: list[str] = []
        wall = wall or pd.Timestamp.now(tz=D.TZ)
        day = str(D.TradingDay(pd.DatetimeIndex([v.now]))[0].date())
        live = set(self.cfg.live_books)

        if self.state.get("killed_day") == day:
            return out
        if not MarketOpen(wall):
            return out

        pos = self.broker.Position()
        groups = self.broker.Groups()
        hour = wall.tz_convert(D.TZ).hour + wall.tz_convert(D.TZ).minute / 60

        # stale data: pull resting entries, never trade blind
        if wall - v.now > self.cfg.stale_after:
            for p, g in groups.items():
                if g["state"] == "working":
                    self.broker.Cancel(p)
            self._Note(f"STALE DATA: last bar ended {v.now}; working entries cancelled", out, f"stale:{v.now}")
            self._Save()
            return out

        pnl = self.broker.DayPnl()
        pnl = v.engine_day_pnl if pnl is None else pnl
        if pnl <= -self.cfg.daily_loss_limit:
            self._Kill(f"day P&L {pnl:.0f} <= -{self.cfg.daily_loss_limit:.0f}", day, out)
            self._Save()
            return out

        # end of day / book flat times the broker brackets don't know about
        flat_at = self.cfg.flatten_time
        early = H.EarlyClose(wall.tz_convert(D.TZ).date())
        if early is not None:
            flat_at = min(flat_at, early - self.cfg.early_close_buffer)
        after_flat = flat_at <= hour < 18
        engine_open = [t for t in v.open_trades if t.book in live]
        book_due = any(v.book_flat.get(t.book) is not None and v.book_flat[t.book] <= hour < 18
                       for t in engine_open)
        if pos and (after_flat or book_due or any(e["book"] in live for e in v.forced_exits)):
            self.broker.Flatten()
            self._Note(f"FLATTEN position {pos} ({'end of day' if after_flat else 'book flat time'})", out)
            self._Save()
            return out
        if after_flat:
            for p, g in groups.items():
                if g["state"] == "working":
                    self.broker.Cancel(p)
            self._Save()
            return out

        # confirmation books: the engine entered at this bar's close -> market bracket now
        for t in engine_open:
            key = f"{t.book}:{t.side}:{t.entry_time}"
            if t.book in v.confirm_books and t.entry_time == v.last_bar and pos == 0 and key not in self.state["orders"]:
                pad = 2 * self.cfg.market_slip_ticks * 0.25
                r_mult = t.side * (t.tp1 - t.entry) / abs(t.entry - t.cur_stop)
                if self._Place(key, t.book, t.side, t.qty, None, t.cur_stop, RoundOut(t.tp1 + t.side * pad, t.side),
                               v.last_close, out, ref_entry=t.entry, r_mult=r_mult):
                    pos = t.side  # treat as filled for the OCA logic below
                    self._ReanchorTargets(out)

        # one position at a time: a live position cancels every other working entry
        if pos:
            self._ReanchorTargets(out)
            for p, g in groups.items():
                if g["state"] == "working":
                    self.broker.Cancel(p)
                    self._Note(f"CANCEL {p}: position open (one-at-a-time)", out)
            for t in engine_open:
                self._Note(f"in position {pos}: {t.book} {'LONG' if t.side > 0 else 'SHORT'} "
                           f"stop {RoundTick(t.cur_stop)} target {RoundTick(t.tp1)}", out,
                           f"inpos:{t.book}:{t.entry_time}")
            self._Save()
            return out

        for t in engine_open:
            self._Note(f"engine is in a {t.book} trade the broker did not fill (touch without fill "
                       f"or order not resting yet); not chasing", out, f"missed:{t.book}:{t.entry_time}")

        # resting limits = engine's armed list for live limit books
        desired = {} if engine_open else {
            o["key"]: o for o in v.armed if o["book"] in live and not o.get("confirm")}
        by_prefix = {rec["prefix"]: k for k, rec in self.state["orders"].items() if "prefix" in rec}
        for p, g in groups.items():
            k = by_prefix.get(p)
            if g["state"] == "working" and (k is None or k not in desired):
                self.broker.Cancel(p)
                if k:
                    self.state["orders"][k]["status"] = "cancelled"
                self._Note(f"CANCEL {p} ({k or 'unknown order'}): no longer armed", out)
        for k, o in desired.items():
            rec = self.state["orders"].get(k)
            if rec and rec.get("prefix") in groups and _Moved(rec.get("levels"), o):
                # the OTE level of a signal moves while its leg develops; the backtest
                # always uses the current level, so re-price the resting order
                self.broker.Cancel(rec["prefix"])
                rec["status"] = "cancelled"
                self._Note(f"REPRICE {k}: {rec.get('levels')} -> {[o['entry'], o['stop'], o['tp1']]}", out)
                groups = {p: g for p, g in groups.items() if p != rec["prefix"]}
            if rec and (rec.get("prefix") in groups or rec.get("status") != "cancelled"):
                # resting already, or it vanished without us cancelling it (filled / rejected):
                # never re-send the same signal
                continue
            side = 1 if o["side"] == "LONG" else -1
            self._Place(k, o["book"], side, o["mnq"], o["entry"], o["stop"], o["tp1"], v.last_close, out)
        self._Save()
        return out


def _Moved(levels, o: dict) -> bool:
    """Entry moved by a tick, or stop/target by 2+ ticks (ignore 1-tick ATR jitter)."""
    if not levels:
        return False
    e, s, t = levels
    return (abs(o["entry"] - e) >= 0.25 - 1e-9 or abs(o["stop"] - s) >= 0.5 - 1e-9
            or abs(o["tp1"] - t) >= 0.5 - 1e-9)
