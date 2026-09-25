"""Checks the live signal logic: armed orders match what the backtest fills, and signal names stay stable as the data window moves."""
import copy

import pandas as pd

from rapier.backtest import Market, Run
from rapier.live import ArmedOrders
from rapier.system import DEFAULT_PARAMS, Build

from test_engine import Synth


def TestLiveAnnouncesEveryBacktestLimitFill():
    """Live LIMIT orders must match what the backtest later fills (no drift)."""
    p = copy.deepcopy(DEFAULT_PARAMS)
    p["swing"].update(confirm=False, bias=[], sessions="any")
    p["intraday"].update(confirm=False, bias=[], sessions="any", min_leg=1.0)
    p["scalp"]["enabled"] = False
    p["risk"].update(flatten_eod=False, daily_loss_limit=1e9, max_open=3)
    df = Synth(n=1500, seed=11)
    books, risk = Build(p)
    trades = Run(Market.From1h(df), books, risk).trades
    assert len(trades) >= 5
    for _, tr in trades.head(8).iterrows():
        m = Market.From1h(df[df.index < pd.Timestamp(tr.entry_time)])
        orders = [o for o in ArmedOrders(m, p) if o["book"] == tr.book]
        side = "LONG" if tr.side > 0 else "SHORT"
        # longs may fill better than the limit (gap through at the open)
        assert any(o["side"] == side and tr.side * (o["entry"] - tr.entry) >= -0.26 for o in orders), tr.entry_time


def TestEveryArmedOrderIsOneTheBacktestWouldFill():
    """Precision: if price trades through an armed limit on the next bar, the backtest
    enters there. Sessions, per-day caps and one-position-at-a-time are active, so
    any entry gate missing from armed_orders shows up as an unfilled 'armed' order."""
    p = copy.deepcopy(DEFAULT_PARAMS)
    p["swing"]["enabled"] = False
    p["intraday"].update(confirm=False, bias=[], sessions="ny", min_leg=1.0)
    p["scalp"]["enabled"] = False
    p["risk"].update(flatten_eod=True, daily_loss_limit=500, max_open=1)
    df = Synth(n=1400, seed=21)
    books, risk = Build(p)
    bt = Run(Market.From1h(df), books, risk).trades
    entries = set(pd.to_datetime(bt.entry_time))
    touched_bars = 0
    for cut in range(700, 1400):
        nxt = df.iloc[cut]
        armed = ArmedOrders(Market.From1h(df.iloc[:cut]), p)
        hit = [o for o in armed if (nxt.low <= o["entry"] - 0.25 if o["side"] == "LONG" else nxt.high >= o["entry"] + 0.25)]
        if hit:
            touched_bars += 1
            assert df.index[cut] in entries, f"armed {hit} at {df.index[cut]} but the backtest did not enter"
    assert touched_bars >= 5
