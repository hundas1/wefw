import copy

import pandas as pd

from rapier.backtest import Market, run
from rapier.live import armed_orders
from rapier.system import DEFAULT_PARAMS, build

from test_engine import synth


def test_live_announces_every_backtest_limit_fill():
    """Live LIMIT orders must match what the backtest later fills (no drift)."""
    p = copy.deepcopy(DEFAULT_PARAMS)
    p["swing"].update(confirm=False, bias=[], sessions="any")
    p["intraday"].update(confirm=False, bias=[], sessions="any", min_leg=1.0)
    p["scalp"]["enabled"] = False
    p["risk"].update(flatten_eod=False, daily_loss_limit=1e9, max_open=3)
    df = synth(n=1500, seed=11)
    books, risk = build(p)
    trades = run(Market.from_1h(df), books, risk).trades
    assert len(trades) >= 5
    for _, tr in trades.head(8).iterrows():
        m = Market.from_1h(df[df.index < pd.Timestamp(tr.entry_time)])
        orders = [o for o in armed_orders(m, p) if o["book"] == tr.book]
        side = "LONG" if tr.side > 0 else "SHORT"
        # longs may fill better than the limit (gap through at the open)
        assert any(o["side"] == side and tr.side * (o["entry"] - tr.entry) >= -0.26 for o in orders), tr.entry_time


def test_every_armed_order_is_one_the_backtest_would_fill():
    """Precision: if price trades through an armed limit on the next bar, the backtest
    enters there. Sessions, per-day caps and one-position-at-a-time are active, so
    any entry gate missing from armed_orders shows up as an unfilled 'armed' order."""
    p = copy.deepcopy(DEFAULT_PARAMS)
    p["swing"]["enabled"] = False
    p["intraday"].update(confirm=False, bias=[], sessions="ny", min_leg=1.0)
    p["scalp"]["enabled"] = False
    p["risk"].update(flatten_eod=True, daily_loss_limit=500, max_open=1)
    df = synth(n=1400, seed=21)
    books, risk = build(p)
    bt = run(Market.from_1h(df), books, risk).trades
    entries = set(pd.to_datetime(bt.entry_time))
    touched_bars = 0
    for cut in range(700, 1400):
        nxt = df.iloc[cut]
        armed = armed_orders(Market.from_1h(df.iloc[:cut]), p)
        hit = [o for o in armed if (nxt.low <= o["entry"] - 0.25 if o["side"] == "LONG" else nxt.high >= o["entry"] + 0.25)]
        if hit:
            touched_bars += 1
            assert df.index[cut] in entries, f"armed {hit} at {df.index[cut]} but the backtest did not enter"
    assert touched_bars >= 5
