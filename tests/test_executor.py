"""Checks the executor (backtest answer -> real orders): safety rails, one-position rule, repricing, restarts, and market-entry target fixes."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from rapier import data as D
from rapier.brokers.base import DryRunBroker, SimBroker, net_position, prefix_for
from rapier.executor import ExecConfig, Executor, View, market_open

T0 = pd.Timestamp("2026-09-15 10:00", tz=D.TZ)  # a Tuesday, NY session


def limit(key="ote-1h:1:100", side="LONG", entry=20000.0, stop=19980.0, tp1=20020.0, mnq=4, book="ote-1h"):
    return {"type": "LIMIT", "book": book, "side": side, "entry": entry, "stop": stop, "tp1": tp1,
            "mnq": mnq, "confirm": False, "key": key}


def view(t=T0, armed=(), open_trades=(), forced=(), pnl=0.0, close=20010.0, **kw):
    return View(now=t + pd.Timedelta("1min"), last_bar=t, last_close=close, armed=list(armed),
                open_trades=list(open_trades), forced_exits=list(forced), engine_day_pnl=pnl, **kw)


def wall(t=T0):
    return t + pd.Timedelta("1min") + pd.Timedelta(seconds=3)


def test_places_and_cancels_resting_limits():
    b = SimBroker()
    ex = Executor(b)
    out = ex.step(view(armed=[limit()]), wall())
    assert any(m.startswith("PLACE ote-1h BUY x4") for m in out)
    assert list(b.groups()) == [prefix_for("ote-1h:1:100")]
    # same signal next bar: nothing new is sent
    assert ex.step(view(T0 + pd.Timedelta("1min"), armed=[limit()]), wall(T0 + pd.Timedelta("1min"))) == []
    # signal disappears -> cancelled
    out = ex.step(view(T0 + pd.Timedelta("2min")), wall(T0 + pd.Timedelta("2min")))
    assert any(m.startswith("CANCEL") for m in out) and b.groups() == {}


def test_filled_signal_is_never_resent():
    b = SimBroker()
    ex = Executor(b)
    ex.step(view(armed=[limit()]), wall())
    b.on_bar(T0 + pd.Timedelta("1min"), 20005, 20006, 19999, 20001)  # trades through -> filled
    assert b.position() == 4
    b.on_bar(T0 + pd.Timedelta("2min"), 20001, 20025, 20000, 20022)  # target hit
    assert b.position() == 0 and b.trades[-1]["reason"] == "target"
    # engine (which may not count a touch fill) still arms the same key: must not re-send
    out = ex.step(view(T0 + pd.Timedelta("3min"), armed=[limit()]), wall(T0 + pd.Timedelta("3min")))
    assert not any(m.startswith("PLACE") for m in out)


def test_resting_order_is_repriced_when_the_ote_level_moves():
    b = SimBroker()
    ex = Executor(b)
    ex.step(view(armed=[limit()]), wall())
    first = list(b.groups())[0]
    # 1-tick stop jitter: keep the order
    t1 = T0 + pd.Timedelta("1min")
    assert ex.step(view(t1, armed=[limit(stop=19980.25)]), wall(t1)) == []
    # the leg extended: entry moved -> cancel and re-place at the new level
    t2 = T0 + pd.Timedelta("2min")
    out = ex.step(view(t2, armed=[limit(entry=20004.0, stop=19984.0, tp1=20024.0)]), wall(t2))
    assert any(m.startswith("REPRICE") for m in out) and any(m.startswith("PLACE") for m in out)
    (new,) = b.groups()
    assert new != first and b._g[new].entry == 20004.0 and b._g[first].state == "cancelled"


def test_one_position_at_a_time_cancels_other_entries():
    b = SimBroker()
    ex = Executor(b, ExecConfig(live_books=("ote-1h", "teacher-1m")))
    ex.step(view(armed=[limit(), limit("teacher-1m:-1:5", "SHORT", 20030, 20060, 20000, book="teacher-1m")]), wall())
    assert len(b.groups()) == 2
    b.on_bar(T0 + pd.Timedelta("1min"), 20005, 20006, 19999, 20001)  # long fills; OCA cancels the short
    assert b.position() == 4 and len([g for g in b.groups().values() if g["state"] == "working"]) == 0


def test_kill_switch_flattens_and_stays_off_for_the_day():
    b = SimBroker()
    ex = Executor(b, ExecConfig(daily_loss_limit=600))
    ex.step(view(armed=[limit()]), wall())
    out = ex.step(view(T0 + pd.Timedelta("1min"), armed=[limit("ote-1h:1:200")], pnl=-650),
                  wall(T0 + pd.Timedelta("1min")))
    assert any("KILL SWITCH" in m for m in out)
    assert ex.step(view(T0 + pd.Timedelta("5min"), armed=[limit("ote-1h:1:300")]), wall(T0 + pd.Timedelta("5min"))) == []
    # next trading day it is back on
    nxt = T0 + pd.Timedelta("1D")
    assert any(m.startswith("PLACE") for m in ex.step(view(nxt, armed=[limit("ote-1h:1:400")]), wall(nxt)))


def test_stale_data_cancels_working_entries_and_blocks_new_ones():
    b = SimBroker()
    ex = Executor(b)
    ex.step(view(armed=[limit()]), wall())
    late = wall() + pd.Timedelta("10min")
    out = ex.step(view(armed=[limit("ote-1h:1:999")]), late)
    assert any("STALE" in m for m in out) and b.groups() == {}


def test_end_of_day_flatten_and_market_closed():
    b = SimBroker()
    ex = Executor(b)
    b.place_bracket("rp-x", 1, 2, None, 19900, 20100)
    b.on_bar(T0, 20000, 20001, 19999, 20000)
    assert b.position() == 2
    t = pd.Timestamp("2026-09-15 16:41", tz=D.TZ)
    out = ex.step(view(t), wall(t))
    assert any("FLATTEN" in m for m in out)
    assert not market_open(pd.Timestamp("2026-09-19 12:00", tz=D.TZ))  # Saturday
    assert not market_open(pd.Timestamp("2026-09-15 17:30", tz=D.TZ))  # daily halt


def test_book_flat_time_flattens_before_engine_bar():
    b = SimBroker()
    ex = Executor(b, ExecConfig(live_books=("teacher-1m",)))
    b.place_bracket("rp-y", -1, 3, None, 20100, 19900)
    b.on_bar(T0, 20000, 20001, 19999, 20000)
    t = pd.Timestamp("2026-09-15 11:30", tz=D.TZ)
    tr = SimpleNamespace(book="teacher-1m", side=-1, qty=3, cur_stop=20100, tp1=19900, entry_time=T0)
    out = ex.step(view(t - pd.Timedelta("1min"), open_trades=[tr], book_flat={"teacher-1m": 11.5}), wall(t - pd.Timedelta("1min")))
    assert any("FLATTEN" in m for m in out)


def test_confirm_book_sends_market_bracket_on_entry_bar():
    b = SimBroker()
    ex = Executor(b, ExecConfig(live_books=("scalp-5m",)))
    tr = SimpleNamespace(book="scalp-5m", side=1, qty=5, entry=20010.25, cur_stop=19980.0, tp1=20040.5, entry_time=T0)
    out = ex.step(view(open_trades=[tr], confirm_books=frozenset({"scalp-5m"})), wall())
    assert any("entry=MKT" in m for m in out)
    # target widened by 2 x 2 ticks: still >= 1R if the market fill is 2 ticks worse
    (g,) = b._g.values()
    assert g.target == 20041.5
    worst_fill = tr.entry + 0.5
    assert g.target - worst_fill >= worst_fill - g.stop
    b.on_bar(T0 + pd.Timedelta("1min"), 20006, 20008, 20004, 20007)
    assert b.position() == 5 and b.trades == []


def test_sanity_refusals_and_size_conversion():
    b = DryRunBroker(units_per_contract=10)  # trading full NQ: 4 MNQ rounds down to 0
    ex = Executor(b)
    out = ex.step(view(armed=[limit(mnq=4)]), wall())
    assert any("below one" in m for m in out) and b.log == []
    far = limit("ote-1h:1:7", entry=25000, stop=24980, tp1=25020, mnq=40)
    out = ex.step(view(armed=[far]), wall())
    assert any("REFUSED" in m for m in out)
    ok = limit("ote-1h:1:8", mnq=37)
    ex.step(view(armed=[ok]), wall())
    assert b.log[-1][:4] == ("place", prefix_for("ote-1h:1:8"), 1, 3)


def test_swing_book_cannot_be_armed_live():
    with pytest.raises(ValueError):
        ExecConfig(live_books=("swing-4h",))


def test_tick_bracket_keeps_at_least_1r():
    from rapier.brokers.base import round_out, tick_bracket

    # the case replay caught: independent rounding left the target 0.25 short of 1R
    e, s, t = tick_bracket(1, 29391.89625, 29370.867875, 1.0)
    assert (e, s, t) == (29392.0, 29370.75, 29413.25) and t - e >= e - s
    e, s, t = tick_bracket(-1, 29464.70625, 29485.807175, 1.0)
    assert s - e <= e - t and all(round(x * 4) == x * 4 for x in (e, s, t))
    assert round_out(100.1, 1) == 100.25 and round_out(100.1, -1) == 100.0


def test_signal_keys_survive_a_sliding_data_window():
    """Keys use the anchor timestamp, so trimming old bars must not rename live signals."""
    import copy

    from rapier.backtest import Market
    from rapier.live import armed_orders
    from rapier.system import DEFAULT_PARAMS

    from test_engine import synth

    p = copy.deepcopy(DEFAULT_PARAMS)
    p["swing"].update(confirm=False, bias=[], sessions="any")
    p["intraday"].update(confirm=False, bias=[], sessions="any", min_leg=1.0)
    p["scalp"]["enabled"] = False
    df = synth(n=1500, seed=11)
    checked = 0
    for cut in range(1200, 1500, 7):
        full = {o["key"] for o in armed_orders(Market.from_1h(df.iloc[:cut]), p)}
        if not full:
            continue
        trimmed = {o["key"] for o in armed_orders(Market.from_1h(df.iloc[60:cut]), p)}
        assert full == trimmed, cut  # same bar, later window start -> same keys
        checked += 1
        if checked == 5:
            break
    assert checked == 5


def test_net_position_parsing():
    by_id = [{"instrument_id": "a", "quantity": 2, "side": "SHORT"}, {"instrument_id": "b", "quantity": 5}]
    assert net_position(by_id, "a", "MNQ") == -2
    by_sym = [{"symbol": "/MNQZ26", "net_quantity": 3}, {"symbol": "/NQZ26", "net_quantity": -1}]
    assert net_position(by_sym, None, "MNQ") == 3
    assert net_position(by_sym, None, "NQ") == -1  # a micro position is never counted as NQ


def test_state_persists_across_restart(tmp_path):
    p = tmp_path / "s.json"
    b = DryRunBroker()
    Executor(b, state_path=p).step(view(armed=[limit()]), wall())
    assert json.loads(p.read_text())["orders"]["ote-1h:1:100"]["prefix"] == prefix_for("ote-1h:1:100")
    ex2 = Executor(b, state_path=p)
    assert ex2.step(view(armed=[limit()]), wall()) == []


def _market_entry(broker, open_px):
    ex = Executor(broker, ExecConfig(live_books=("scalp-5m",)))
    tr = SimpleNamespace(book="scalp-5m", side=1, qty=5, entry=20010.25, cur_stop=19980.0, tp1=20040.5, entry_time=T0)
    ex.step(view(open_trades=[tr], confirm_books=frozenset({"scalp-5m"})), wall())
    if isinstance(broker, SimBroker):
        broker.on_bar(T0 + pd.Timedelta("1min"), open_px, open_px + 0.5, open_px - 0.5, open_px)
    t1 = T0 + pd.Timedelta("1min")
    return ex, tr, ex.step(view(t1, open_trades=[tr], confirm_books=frozenset({"scalp-5m"})), wall(t1))


def test_market_target_reanchored_on_adverse_fill():
    # FX Replay case: a market entry filled 17 ticks worse than the engine price -> 0.87R
    b = SimBroker()
    ex, tr, out = _market_entry(b, 20014.25)
    (g,) = b._g.values()
    assert g.fill == 20014.5
    assert any(m.startswith("AMEND") for m in out)
    assert g.target - g.fill >= g.fill - g.stop  # >= 1R from the real fill
    assert g.target == 20049.0
    # done once: a later cycle leaves it alone
    t2 = T0 + pd.Timedelta("2min")
    assert not any("AMEND" in m for m in ex.step(view(t2, open_trades=[tr]), wall(t2)))


def test_market_target_pad_removed_on_good_fill():
    b = SimBroker()
    _, _, out = _market_entry(b, 20008.0)
    (g,) = b._g.values()
    assert g.target - g.fill == pytest.approx(g.fill - g.stop, abs=0.25)


def test_brokers_without_amend_keep_the_static_pad():
    b = DryRunBroker()
    _, _, out = _market_entry(b, 20014.25)
    assert not any("AMEND" in m for m in out)
    assert b.log[0][-1] == 20041.5  # tp1 + 2 x 2 ticks
