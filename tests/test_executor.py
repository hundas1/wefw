"""Checks the executor (backtest answer -> real orders): safety rails, one-position rule, repricing, restarts, and market-entry target fixes."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from rapier import data as D
from rapier.brokers.base import DryRunBroker, SimBroker, NetPosition, PrefixFor
from rapier.executor import ExecConfig, Executor, View, MarketOpen

T0 = pd.Timestamp("2026-09-15 10:00", tz=D.TZ)  # a Tuesday, NY session


def MakeLimit(key="ote-1h:1:100", side="LONG", entry=20000.0, stop=19980.0, tp1=20020.0, mnq=4, book="ote-1h"):
    return {"type": "LIMIT", "book": book, "side": side, "entry": entry, "stop": stop, "tp1": tp1,
            "mnq": mnq, "confirm": False, "key": key}


def MakeView(t=T0, armed=(), open_trades=(), forced=(), pnl=0.0, close=20010.0, **kw):
    return View(now=t + pd.Timedelta("1min"), last_bar=t, last_close=close, armed=list(armed),
                open_trades=list(open_trades), forced_exits=list(forced), engine_day_pnl=pnl, **kw)


def Wall(t=T0):
    return t + pd.Timedelta("1min") + pd.Timedelta(seconds=3)


def TestPlacesAndCancelsRestingLimits():
    b = SimBroker()
    ex = Executor(b)
    out = ex.Step(MakeView(armed=[MakeLimit()]), Wall())
    assert any(m.startswith("PLACE ote-1h BUY x4") for m in out)
    assert list(b.Groups()) == [PrefixFor("ote-1h:1:100")]
    # same signal next bar: nothing new is sent
    assert ex.Step(MakeView(T0 + pd.Timedelta("1min"), armed=[MakeLimit()]), Wall(T0 + pd.Timedelta("1min"))) == []
    # signal disappears -> cancelled
    out = ex.Step(MakeView(T0 + pd.Timedelta("2min")), Wall(T0 + pd.Timedelta("2min")))
    assert any(m.startswith("CANCEL") for m in out) and b.Groups() == {}


def TestFilledSignalIsNeverResent():
    b = SimBroker()
    ex = Executor(b)
    ex.Step(MakeView(armed=[MakeLimit()]), Wall())
    b.OnBar(T0 + pd.Timedelta("1min"), 20005, 20006, 19999, 20001)  # trades through -> filled
    assert b.Position() == 4
    b.OnBar(T0 + pd.Timedelta("2min"), 20001, 20025, 20000, 20022)  # target hit
    assert b.Position() == 0 and b.trades[-1]["reason"] == "target"
    # engine (which may not count a touch fill) still arms the same key: must not re-send
    out = ex.Step(MakeView(T0 + pd.Timedelta("3min"), armed=[MakeLimit()]), Wall(T0 + pd.Timedelta("3min")))
    assert not any(m.startswith("PLACE") for m in out)


def TestRestingOrderIsRepricedWhenTheOteLevelMoves():
    b = SimBroker()
    ex = Executor(b)
    ex.Step(MakeView(armed=[MakeLimit()]), Wall())
    first = list(b.Groups())[0]
    # 1-tick stop jitter: keep the order
    t1 = T0 + pd.Timedelta("1min")
    assert ex.Step(MakeView(t1, armed=[MakeLimit(stop=19980.25)]), Wall(t1)) == []
    # the leg extended: entry moved -> cancel and re-place at the new level
    t2 = T0 + pd.Timedelta("2min")
    out = ex.Step(MakeView(t2, armed=[MakeLimit(entry=20004.0, stop=19984.0, tp1=20024.0)]), Wall(t2))
    assert any(m.startswith("REPRICE") for m in out) and any(m.startswith("PLACE") for m in out)
    (new,) = b.Groups()
    assert new != first and b._g[new].entry == 20004.0 and b._g[first].state == "cancelled"


def TestOnePositionAtATimeCancelsOtherEntries():
    b = SimBroker()
    ex = Executor(b, ExecConfig(live_books=("ote-1h", "teacher-1m")))
    ex.Step(MakeView(armed=[MakeLimit(), MakeLimit("teacher-1m:-1:5", "SHORT", 20030, 20060, 20000, book="teacher-1m")]), Wall())
    assert len(b.Groups()) == 2
    b.OnBar(T0 + pd.Timedelta("1min"), 20005, 20006, 19999, 20001)  # long fills; OCA cancels the short
    assert b.Position() == 4 and len([g for g in b.Groups().values() if g["state"] == "working"]) == 0


def TestKillSwitchFlattensAndStaysOffForTheDay():
    b = SimBroker()
    ex = Executor(b, ExecConfig(daily_loss_limit=600))
    ex.Step(MakeView(armed=[MakeLimit()]), Wall())
    out = ex.Step(MakeView(T0 + pd.Timedelta("1min"), armed=[MakeLimit("ote-1h:1:200")], pnl=-650),
                  Wall(T0 + pd.Timedelta("1min")))
    assert any("KILL SWITCH" in m for m in out)
    assert ex.Step(MakeView(T0 + pd.Timedelta("5min"), armed=[MakeLimit("ote-1h:1:300")]), Wall(T0 + pd.Timedelta("5min"))) == []
    # next trading day it is back on
    nxt = T0 + pd.Timedelta("1D")
    assert any(m.startswith("PLACE") for m in ex.Step(MakeView(nxt, armed=[MakeLimit("ote-1h:1:400")]), Wall(nxt)))


def TestStaleDataCancelsWorkingEntriesAndBlocksNewOnes():
    b = SimBroker()
    ex = Executor(b)
    ex.Step(MakeView(armed=[MakeLimit()]), Wall())
    late = Wall() + pd.Timedelta("10min")
    out = ex.Step(MakeView(armed=[MakeLimit("ote-1h:1:999")]), late)
    assert any("STALE" in m for m in out) and b.Groups() == {}


def TestEndOfDayFlattenAndMarketClosed():
    b = SimBroker()
    ex = Executor(b)
    b.PlaceBracket("rp-x", 1, 2, None, 19900, 20100)
    b.OnBar(T0, 20000, 20001, 19999, 20000)
    assert b.Position() == 2
    t = pd.Timestamp("2026-09-15 16:41", tz=D.TZ)
    out = ex.Step(MakeView(t), Wall(t))
    assert any("FLATTEN" in m for m in out)
    assert not MarketOpen(pd.Timestamp("2026-09-19 12:00", tz=D.TZ))  # Saturday
    assert not MarketOpen(pd.Timestamp("2026-09-15 17:30", tz=D.TZ))  # daily halt


def TestBookFlatTimeFlattensBeforeEngineBar():
    b = SimBroker()
    ex = Executor(b, ExecConfig(live_books=("teacher-1m",)))
    b.PlaceBracket("rp-y", -1, 3, None, 20100, 19900)
    b.OnBar(T0, 20000, 20001, 19999, 20000)
    t = pd.Timestamp("2026-09-15 11:30", tz=D.TZ)
    tr = SimpleNamespace(book="teacher-1m", side=-1, qty=3, cur_stop=20100, tp1=19900, entry_time=T0)
    out = ex.Step(MakeView(t - pd.Timedelta("1min"), open_trades=[tr], book_flat={"teacher-1m": 11.5}), Wall(t - pd.Timedelta("1min")))
    assert any("FLATTEN" in m for m in out)


def TestConfirmBookSendsMarketBracketOnEntryBar():
    b = SimBroker()
    ex = Executor(b, ExecConfig(live_books=("scalp-5m",)))
    tr = SimpleNamespace(book="scalp-5m", side=1, qty=5, entry=20010.25, cur_stop=19980.0, tp1=20040.5, entry_time=T0)
    out = ex.Step(MakeView(open_trades=[tr], confirm_books=frozenset({"scalp-5m"})), Wall())
    assert any("entry=MKT" in m for m in out)
    # target widened by 2 x 2 ticks: still >= 1R if the market fill is 2 ticks worse
    (g,) = b._g.values()
    assert g.target == 20041.5
    worst_fill = tr.entry + 0.5
    assert g.target - worst_fill >= worst_fill - g.stop
    b.OnBar(T0 + pd.Timedelta("1min"), 20006, 20008, 20004, 20007)
    assert b.Position() == 5 and b.trades == []


def TestSanityRefusalsAndSizeConversion():
    b = DryRunBroker(units_per_contract=10)  # trading full NQ: 4 MNQ rounds down to 0
    ex = Executor(b)
    out = ex.Step(MakeView(armed=[MakeLimit(mnq=4)]), Wall())
    assert any("below one" in m for m in out) and b.log == []
    far = MakeLimit("ote-1h:1:7", entry=25000, stop=24980, tp1=25020, mnq=40)
    out = ex.Step(MakeView(armed=[far]), Wall())
    assert any("REFUSED" in m for m in out)
    ok = MakeLimit("ote-1h:1:8", mnq=37)
    ex.Step(MakeView(armed=[ok]), Wall())
    assert b.log[-1][:4] == ("place", PrefixFor("ote-1h:1:8"), 1, 3)


def TestSwingBookCannotBeArmedLive():
    with pytest.raises(ValueError):
        ExecConfig(live_books=("swing-4h",))


def TestTickBracketKeepsAtLeast1r():
    from rapier.brokers.base import RoundOut, TickBracket

    # the case replay caught: independent rounding left the target 0.25 short of 1R
    e, s, t = TickBracket(1, 29391.89625, 29370.867875, 1.0)
    assert (e, s, t) == (29392.0, 29370.75, 29413.25) and t - e >= e - s
    e, s, t = TickBracket(-1, 29464.70625, 29485.807175, 1.0)
    assert s - e <= e - t and all(round(x * 4) == x * 4 for x in (e, s, t))
    assert RoundOut(100.1, 1) == 100.25 and RoundOut(100.1, -1) == 100.0


def TestSignalKeysSurviveASlidingDataWindow():
    """Keys use the anchor timestamp, so trimming old bars must not rename live signals."""
    import copy

    from rapier.backtest import Market
    from rapier.live import ArmedOrders
    from rapier.system import DEFAULT_PARAMS

    from test_engine import Synth

    p = copy.deepcopy(DEFAULT_PARAMS)
    p["swing"].update(confirm=False, bias=[], sessions="any")
    p["intraday"].update(confirm=False, bias=[], sessions="any", min_leg=1.0)
    p["scalp"]["enabled"] = False
    df = Synth(n=1500, seed=11)
    checked = 0
    for cut in range(1200, 1500, 7):
        full = {o["key"] for o in ArmedOrders(Market.From1h(df.iloc[:cut]), p)}
        if not full:
            continue
        trimmed = {o["key"] for o in ArmedOrders(Market.From1h(df.iloc[60:cut]), p)}
        assert full == trimmed, cut  # same bar, later window start -> same keys
        checked += 1
        if checked == 5:
            break
    assert checked == 5


def TestNetPositionParsing():
    by_id = [{"instrument_id": "a", "quantity": 2, "side": "SHORT"}, {"instrument_id": "b", "quantity": 5}]
    assert NetPosition(by_id, "a", "MNQ") == -2
    by_sym = [{"symbol": "/MNQZ26", "net_quantity": 3}, {"symbol": "/NQZ26", "net_quantity": -1}]
    assert NetPosition(by_sym, None, "MNQ") == 3
    assert NetPosition(by_sym, None, "NQ") == -1  # a micro position is never counted as NQ


def TestStatePersistsAcrossRestart(tmp_path):
    p = tmp_path / "s.json"
    b = DryRunBroker()
    Executor(b, state_path=p).Step(MakeView(armed=[MakeLimit()]), Wall())
    assert json.loads(p.read_text())["orders"]["ote-1h:1:100"]["prefix"] == PrefixFor("ote-1h:1:100")
    ex2 = Executor(b, state_path=p)
    assert ex2.Step(MakeView(armed=[MakeLimit()]), Wall()) == []


def _MarketEntry(broker, open_px):
    ex = Executor(broker, ExecConfig(live_books=("scalp-5m",)))
    tr = SimpleNamespace(book="scalp-5m", side=1, qty=5, entry=20010.25, cur_stop=19980.0, tp1=20040.5, entry_time=T0)
    ex.Step(MakeView(open_trades=[tr], confirm_books=frozenset({"scalp-5m"})), Wall())
    if isinstance(broker, SimBroker):
        broker.OnBar(T0 + pd.Timedelta("1min"), open_px, open_px + 0.5, open_px - 0.5, open_px)
    t1 = T0 + pd.Timedelta("1min")
    return ex, tr, ex.Step(MakeView(t1, open_trades=[tr], confirm_books=frozenset({"scalp-5m"})), Wall(t1))


def TestMarketTargetReanchoredOnAdverseFill():
    # FX Replay case: a market entry filled 17 ticks worse than the engine price -> 0.87R
    b = SimBroker()
    ex, tr, out = _MarketEntry(b, 20014.25)
    (g,) = b._g.values()
    assert g.fill == 20014.5
    assert any(m.startswith("AMEND") for m in out)
    assert g.target - g.fill >= g.fill - g.stop  # >= 1R from the real fill
    assert g.target == 20049.0
    # done once: a later cycle leaves it alone
    t2 = T0 + pd.Timedelta("2min")
    assert not any("AMEND" in m for m in ex.Step(MakeView(t2, open_trades=[tr]), Wall(t2)))


def TestMarketTargetPadRemovedOnGoodFill():
    b = SimBroker()
    _, _, out = _MarketEntry(b, 20008.0)
    (g,) = b._g.values()
    assert g.target - g.fill == pytest.approx(g.fill - g.stop, abs=0.25)


def TestBrokersWithoutAmendKeepTheStaticPad():
    b = DryRunBroker()
    _, _, out = _MarketEntry(b, 20014.25)
    assert not any("AMEND" in m for m in out)
    assert b.log[0][-1] == 20041.5  # tp1 + 2 x 2 ticks


def TestBlindCycleCancelsEntriesAndStillFlattensAtClose():
    b = SimBroker()
    ex = Executor(b)
    ex.Step(MakeView(armed=[MakeLimit()]), Wall())
    assert list(b.Groups())
    out = ex.Blind(T0 + pd.Timedelta("5min"))
    assert any("NO DATA" in m for m in out) and not b.Groups()
    t1 = T0 + pd.Timedelta("6min")
    assert any(m.startswith("PLACE") for m in ex.Step(MakeView(t1, armed=[MakeLimit()]), Wall(t1)))  # data back
    ex.Blind(T0 + pd.Timedelta("7min"))
    b.PlaceBracket("rp-y", 1, 2, None, 19900, 20100)
    b.OnBar(T0, 20000, 20001, 19999, 20000)
    assert not any("FLATTEN" in m for m in ex.Blind(T0 + pd.Timedelta("10min")))  # mid-session: brackets protect it
    assert any("FLATTEN" in m for m in ex.Blind(pd.Timestamp("2026-09-15 16:45", tz=D.TZ)))
