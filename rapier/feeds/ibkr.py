"""Interactive Brokers (TWS / IB Gateway) market data.

* Connects **read-only**: this connection cannot place orders.
* ``backfill`` downloads 1m bars for each dated quarterly NQ contract
  (IBKR keeps expired futures for 2 years after expiry) and stitches them into
  one back-adjusted continuous series using the *measured* spread between the
  two contracts at each roll, not an estimate.
* ``poll`` fetches the latest bars of the front contract for the live loop.

Roll convention: Rapier switches to the next contract at the Sunday 18:00 ET
open of expiry week, the same switch the Yahoo series it was developed on uses.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .. import data as D

log = logging.getLogger("rapier.ibkr")

RAW_DIR = D.DATA_DIR / "ibkr_raw"
IBKR_1M = D.DATA_DIR / "NQ_IBKR_1m.csv.gz"
ROLLS_JSON = D.DATA_DIR / "NQ_IBKR_rolls.json"
PACE_S = 10.5  # IBKR historical-data pacing: at most 60 requests per 10 minutes


def switch_time(expiry: dt.date) -> pd.Timestamp:
    sunday = expiry - dt.timedelta(days=(expiry.weekday() + 1) % 7)
    return pd.Timestamp(sunday, tz=D.TZ) + pd.Timedelta(hours=18)


def _expiries(start: pd.Timestamp, end: pd.Timestamp) -> list[dt.date]:
    out = []
    for y in range(start.year - 1, end.year + 2):
        for m in (3, 6, 9, 12):
            out.append(D.third_friday(y, m))
    return sorted(out)


def front_expiry(t: pd.Timestamp) -> dt.date:
    """Expiry of the contract Rapier trades at time ``t``."""
    return next(e for e in _expiries(t, t) if switch_time(e) > t)


def schedule(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[dt.date, pd.Timestamp, pd.Timestamp]]:
    """[(expiry, active_from, active_to)] covering [start, end)."""
    ex = _expiries(start, end)
    out = []
    for prev, cur in zip(ex, ex[1:]):
        a, b = switch_time(prev), switch_time(cur)
        if b > start and a < end:
            out.append((cur, max(a, start), min(b, end)))
    return out


def session_ends(a: pd.Timestamp, b: pd.Timestamp) -> list[pd.Timestamp]:
    """17:00 ET close of every weekday session overlapping [a, b)."""
    days = pd.bdate_range(a.tz_convert(D.TZ).normalize().tz_localize(None),
                          (b + pd.Timedelta(hours=7)).tz_convert(D.TZ).normalize().tz_localize(None))
    ends = [pd.Timestamp(d, tz=D.TZ) + pd.Timedelta(hours=17) for d in days]
    return [e for e in ends if e > a and e - pd.Timedelta(hours=23) < b]


def to_frame(bars) -> pd.DataFrame:
    rows = [(b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars or []]
    if not rows:
        return pd.DataFrame(columns=D.COLS, index=pd.DatetimeIndex([], tz=D.TZ, name="time"))
    df = pd.DataFrame(rows, columns=["time", *D.COLS]).set_index("time")
    idx = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
    df.index = idx
    return D._normalize(df)


def stitch(segments: list[tuple[dt.date, pd.DataFrame]], overlaps: dict[dt.date, pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    """Back-adjust contract segments into one continuous series.

    ``overlaps[e]`` holds the *next* contract's bars around the switch out of
    contract ``e``. The roll spread is the median (next - current) close over
    the last 60 common minutes before the switch.
    """
    spreads = {}
    for (e0, s0), (e1, _) in zip(segments, segments[1:]):
        nxt = overlaps.get(e0)
        common = s0["close"].to_frame("a").join(nxt["close"].rename("b"), how="inner") if nxt is not None else None
        if common is None or common.empty:
            raise ValueError(f"no overlapping bars to measure the {e0} -> {e1} roll spread")
        spreads[e0] = round(float((common.b - common.a).tail(60).median()) * 4) / 4
    out = []
    for i, (e, seg) in enumerate(segments):
        adj = sum(spreads[x] for x, _ in segments[i:-1])
        s = seg.copy()
        s[["open", "high", "low", "close"]] += adj
        out.append(s)
    df = pd.concat(out).sort_index()
    return df[~df.index.duplicated(keep="last")], {str(k): v for k, v in spreads.items()}


class IBKRFeed:
    def __init__(self, host: str = "127.0.0.1", port: int = 4001, client_id: int = 71, ib=None,
                 pace: float = PACE_S):
        self.host, self.port, self.client_id, self.ib, self.pace = host, port, client_id, ib, pace
        self._contracts: dict = {}

    # ------------------------------------------------------------- connection
    def connect(self) -> "IBKRFeed":
        if self.ib is None:
            from ib_async import IB

            self.ib = IB()
        if not self.ib.isConnected():
            self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=True, timeout=20)
        return self

    def contract(self, expiry: dt.date, root: str = "NQ"):
        key = (root, expiry)
        if key not in self._contracts:
            from ib_async import Future

            c = Future(root, expiry.strftime("%Y%m"), "CME", currency="USD", includeExpired=True)
            q = self.ib.qualifyContracts(c)
            if not q or q[0] is None:
                raise RuntimeError(f"IBKR could not qualify {root} {expiry:%Y%m}")
            self._contracts[key] = q[0]
        return self._contracts[key]

    def bars(self, contract, end: pd.Timestamp | None, duration: str = "1 D") -> pd.DataFrame:
        end_s = "" if end is None else end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S")
        for attempt in range(4):
            got = self.ib.reqHistoricalData(contract, endDateTime=end_s, durationStr=duration,
                                            barSizeSetting="1 min", whatToShow="TRADES", useRTH=False,
                                            formatDate=2, timeout=120)
            if got:
                return to_frame(got)
            wait = self.pace * (attempt + 1)
            log.warning("empty/failed IBKR history (%s, %s); retry in %.0fs", contract.localSymbol, end_s, wait)
            time.sleep(wait)
        return to_frame([])

    # ---------------------------------------------------------------- backfill
    def _day(self, expiry: dt.date, end: pd.Timestamp, today: pd.Timestamp) -> pd.DataFrame:
        path = RAW_DIR / f"NQ{expiry:%Y%m}_{end:%Y%m%d}.csv"
        if path.exists():
            return D.load_csv(path)
        df = self.bars(self.contract(expiry), end)
        time.sleep(self.pace)
        if end < today and len(df):  # only cache complete sessions
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index_label="time")
        return df

    def backfill(self, start: str | pd.Timestamp, end: str | pd.Timestamp | None = None,
                 progress=print) -> pd.DataFrame:
        """Download (resumable) and stitch continuous 1m NQ; writes ``NQ_IBKR_1m.csv.gz``."""
        start = pd.Timestamp(start, tz=D.TZ) if not isinstance(start, pd.Timestamp) else start
        now = pd.Timestamp.now(tz=D.TZ)
        end = now if end is None else pd.Timestamp(end, tz=D.TZ)
        segments, overlaps = [], {}
        sched = schedule(start, end)
        n_req = sum(len(session_ends(a, b)) for _, a, b in sched)
        progress(f"IBKR backfill {start:%Y-%m-%d} -> {end:%Y-%m-%d}: {len(sched)} contract(s), "
                 f"up to {n_req} daily requests (~{n_req * self.pace / 60:.0f} min if nothing is cached)")
        for k, (expiry, a, b) in enumerate(sched):
            parts = []
            for se in session_ends(a, b):
                parts.append(self._day(expiry, se, now))
            seg = pd.concat(parts) if parts else to_frame([])
            seg = seg[~seg.index.duplicated(keep="last")].sort_index()
            segments.append((expiry, seg[(seg.index >= a) & (seg.index < b)]))
            progress(f"  NQ {expiry:%Y%m}: {len(segments[-1][1])} bars")
            if k + 1 < len(sched):
                # next contract over the last session before this switch, to measure the spread
                last_session = session_ends(a, b)[-1]
                overlaps[expiry] = self._day(sched[k + 1][0], last_session, now)
        df, spreads = stitch(segments, overlaps)
        if IBKR_1M.exists() and not df.empty:
            # keep older stitched history that this run did not cover
            old = _align(D.load_csv(IBKR_1M), df)
            old = old[old.index < df.index[0]]
            if len(old):
                df = pd.concat([old, df])
        IBKR_1M.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(IBKR_1M, compression="gzip", index_label="time")
        prev = json.loads(ROLLS_JSON.read_text()) if ROLLS_JSON.exists() else {}
        ROLLS_JSON.write_text(json.dumps(prev | spreads, indent=1))
        progress(f"wrote {IBKR_1M} ({len(df)} bars, {df.index[0]} -> {df.index[-1]}), roll spreads {spreads}")
        return df

    # -------------------------------------------------------------------- live
    def poll(self, expiry: dt.date, duration: str = "7200 S") -> pd.DataFrame:
        """Latest *completed* 1m bars of the front contract (the forming bar is dropped)."""
        df = self.bars(self.contract(expiry), None, duration)
        if len(df):
            now = pd.Timestamp.now(tz=D.TZ)
            df = df[df.index + pd.Timedelta("1min") <= now]
        return df


def _align(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Shift an older stitched series so it lines up with a freshly stitched one."""
    ov = new["close"].reindex(old.index).dropna()
    if len(ov):
        shift = float(np.median(ov - old["close"].reindex(ov.index)))
        old = old.copy()
        old[["open", "high", "low", "close"]] += round(shift * 4) / 4
    return old


def load_ibkr_1m() -> pd.DataFrame | None:
    return D.load_csv(IBKR_1M) if IBKR_1M.exists() else None
