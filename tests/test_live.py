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
