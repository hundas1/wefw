"""Checks the holiday calendar against CME's published 2026 dates, and that the executor is flat before an early close."""
import datetime as dt

import pandas as pd

from rapier import data as D
from rapier import market_hours as H
from rapier.brokers.base import SimBroker
from rapier.executor import Executor, MarketOpen, View


def TestCalendar2026MatchesCme():
    assert H.ClosedDays(2026) == {dt.date(2026, 1, 1), dt.date(2026, 4, 3), dt.date(2026, 12, 25)}
    e = H.EarlyCloses(2026)
    for d in ("2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26"):
        assert e[dt.date.fromisoformat(d)] == 13.0, d
    assert e[dt.date(2026, 11, 27)] == 13.25 and e[dt.date(2026, 12, 24)] == 13.25
    assert H.EarlyClose(dt.date(2026, 9, 8)) is None
    assert H.IsClosedDay(dt.date(2027, 3, 26))  # Good Friday 2027, computed not listed
    assert H.IsClosedDay(dt.date(2027, 12, 24))  # Christmas on a Saturday -> Friday closed
    assert dt.date(2027, 12, 31) not in H.ClosedDays(2028) and dt.date(2028, 1, 1) in H.ClosedDays(2028)


def TestClosedDayAndEarlyCloseFlatten():
    assert not MarketOpen(pd.Timestamp("2026-04-03 10:00", tz=D.TZ))   # Good Friday
    assert not MarketOpen(pd.Timestamp("2025-12-25 10:00", tz=D.TZ))   # Christmas (Thursday)
    assert MarketOpen(pd.Timestamp("2025-12-25 18:30", tz=D.TZ))       # reopens that evening for Dec 26
    b = SimBroker()
    ex = Executor(b)
    t0 = pd.Timestamp("2026-11-27 12:40", tz=D.TZ)
    b.PlaceBracket("rp-x", 1, 2, None, 19900, 20100)
    b.OnBar(t0, 20000, 20001, 19999, 20000)
    v = lambda t: View(now=t + pd.Timedelta("1min"), last_bar=t, last_close=20000.0, armed=[], open_trades=[],
                       forced_exits=[])
    assert not any("FLATTEN" in m for m in ex.Step(v(t0), t0 + pd.Timedelta("1min")))  # 12:41: still trading
    t1 = pd.Timestamp("2026-11-27 13:04", tz=D.TZ)
    assert any("FLATTEN" in m for m in ex.Step(v(t1), t1 + pd.Timedelta("1min")))      # 13:05 = 13:15 - 10 min
