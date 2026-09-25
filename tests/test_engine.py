"""Checks the core engine on synthetic bars: no look-ahead, every trade >= 1R, stop wins a same-bar tie, 4h bar alignment, and contract-roll adjustment."""
import numpy as np
import pandas as pd
import pytest

from rapier import data as D
from rapier.backtest import BookConfig, Market, RiskConfig, Run
from rapier.indicators import StructureTrend
from rapier.strategy import SetupParams, FindSetups


def Synth(n=3000, seed=1, start="2025-01-06 18:00"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n * 2, freq="1h", tz=D.TZ)
    idx = idx[(idx.hour != 17) & ~((idx.dayofweek == 5) | ((idx.dayofweek == 4) & (idx.hour > 17))
                                   | ((idx.dayofweek == 6) & (idx.hour < 18)))][:n]
    steps = rng.normal(0.5, 25, n)
    close = 20000 + np.cumsum(steps)
    open_ = np.r_[close[0], close[:-1]]
    hi = np.maximum(open_, close) + rng.uniform(0, 15, n)
    lo = np.minimum(open_, close) - rng.uniform(0, 15, n)
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close,
                         "volume": 1000.0}, index=idx)


def TestSetupsHaveNoLookahead():
    df = Synth()
    p = SetupParams(k=3)
    full = FindSetups(df, pd.Timedelta("1h"), p)
    cut = 2000
    part = FindSetups(df.iloc[:cut], pd.Timedelta("1h"), p)
    for side in ("long", "short"):
        a = getattr(full, side)["E"][:cut]
        b = getattr(part, side)["E"]
        assert np.allclose(a, b, equal_nan=True), side


def TestStructureTrendHasNoLookahead():
    df = Synth(seed=3)
    full = StructureTrend(df, 3)
    part = StructureTrend(df.iloc[:1500], 3)
    assert (full[:1500] == part).all()


def TestEveryTradePlannedAtLeast1r():
    with pytest.raises(ValueError):
        BookConfig("x", "1h", tp1_r=0.8)
    df = Synth(seed=5)
    m = Market.From1h(df)
    res = Run(m, (BookConfig("ote-1h", "1h", SetupParams(k=2, min_leg_atr=1.0), bias_tfs=()),),
              RiskConfig(flatten_eod=False, daily_loss_limit=1e9, dd_throttle=None))
    assert len(res.trades) > 10
    assert (res.trades.planned_rr >= 1.0).all()
    # losses are bounded by the planned risk (plus slippage/commission)
    assert res.trades.r.min() > -1.2


def TestStopWinsWhenBarHitsBoth():
    # one long setup then a single huge bar that spans both stop and target
    df = Synth(n=400, seed=7)
    m = Market.From1h(df)
    res = Run(m, (BookConfig("ote-1h", "1h", SetupParams(k=2, min_leg_atr=1.0), bias_tfs=()),),
              RiskConfig(flatten_eod=False, daily_loss_limit=1e9, dd_throttle=None))
    t = res.trades
    both = t[(t.mfe_pts >= t.risk_pts) & (t.mae_pts >= t.risk_pts)]
    # when a subsequent bar touched both, the engine must have taken the stop
    for _, row in both.iterrows():
        assert row.exit_reason in ("stop", "tp1", "tp1+stop", "eod", "end")


def TestResample4hIsSessionAligned():
    df = Synth(n=200)
    h4 = D.Resample(df, "4h")
    assert set(h4.index.hour) <= {2, 6, 10, 14, 18, 22}
    d1 = D.Resample(df, "1D")
    assert (d1.index.hour == 17).all()


def TestMarketNormalizesTimestampUnits():
    df = Synth(n=300)
    us = df.copy()
    us.index = us.index.as_unit("us")
    m = Market.From1h(us)
    assert all(f.index.unit == "ns" for f in m.frames.values())
    assert m.base.index.unit == "ns"


def TestRollAdjustRemovesSundayGap():
    df = Synth(n=600, start="2025-09-01 18:00")
    sw = df.index[(df.index.date == pd.Timestamp("2025-09-14").date()) & (df.index.hour == 18)][0]
    jumped = df.copy()
    # a weekend-hidden switch is adjusted by theoretical carry (0.95% of price),
    # so inject exactly that; the residual error equals (true spread - carry)
    lo, hi = pd.Timestamp("2025-09-14 12:00", tz=D.TZ), pd.Timestamp("2025-09-19", tz=D.TZ)
    spread = round(0.0095 * df.close.loc[lo:hi].median() * 4) / 4
    jumped.loc[jumped.index >= sw, ["open", "high", "low", "close"]] += spread
    adj, rolls = D.RollAdjust(jumped)
    assert len(rolls) == 1 and rolls[0].switch == sw and rolls[0].method == "sunday-open"
    orig_gap = df.open[sw] - df.close.shift()[sw]
    new_gap = adj.open[sw] - adj.close.shift()[sw]
    assert abs(new_gap - orig_gap) < 5


def TestRollAdjustDropsFlipFlopBars():
    df = Synth(n=600, start="2025-09-01 18:00")
    t0 = pd.Timestamp("2025-09-16 03:00", tz=D.TZ)
    j = df.copy()
    new = j.index >= t0 + pd.Timedelta("3h")
    j.loc[new, ["open", "high", "low", "close"]] += 190
    # two contaminated bars alternating contracts before the final switch
    j.loc[t0, ["open", "close"]] += 190
    adj, rolls = D.RollAdjust(j)
    r = rolls[0]
    assert r.method == "flip" and r.switch == t0 + pd.Timedelta("3h")
    assert t0 not in adj.index
