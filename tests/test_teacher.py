"""Checks the owner's 1-minute 'teacher' OTE rules: no look-ahead, impulse size/shape, fixed stop, flat time, breakeven, and rejecting fake (upsampled) 1m data."""
import numpy as np
import pandas as pd

from rapier import data as D
from rapier.backtest import BookConfig, Market, RiskConfig, Run
from rapier.strategy import SetupParams, FindSetups

from test_engine import Synth


def TeacherParams(**kw):
    base = dict(k=2, fib=0.618, min_leg_atr=0, max_leg_atr=1e9, min_leg_pts=40, min_leg_bars=3,
                max_leg_bars=36, max_bar_frac=0.8, require_bos=False, mode="impulse")
    return SetupParams(**(base | kw))


def TestImpulseSetupsHaveNoLookahead():
    df = Synth(n=2500, seed=4)
    full = FindSetups(df, pd.Timedelta("1h"), TeacherParams())
    part = FindSetups(df.iloc[:1700], pd.Timedelta("1h"), TeacherParams())
    for side in ("long", "short"):
        assert np.allclose(getattr(full, side)["E"][:1700], getattr(part, side)["E"], equal_nan=True)
    assert np.isfinite(full.long["E"]).sum() > 20


def TestImpulseShapeRulesAreEnforced():
    df = Synth(n=2500, seed=4)
    p = TeacherParams(min_leg_pts=60, min_leg_bars=5, max_leg_bars=20, max_bar_frac=0.5)
    s = FindSetups(df, pd.Timedelta("1h"), p)
    h, l = df.high.to_numpy(), df.low.to_numpy()
    idx = np.flatnonzero(np.isfinite(s.long["E"]))
    assert len(idx)
    for i in idx:
        o, x, D = int(s.long["id"][i]), int(s.long["x"][i]), s.long["D"][i]
        assert D >= 60 and 5 <= x - o <= 20
        assert (h[o:x + 1] - l[o:x + 1]).max() / D <= 0.5
        assert np.isclose(s.long["E"][i], h[x] - 0.618 * D)


def TestFixedStopAndFlatAfter():
    df = Synth(n=2500, seed=8)
    b = BookConfig("t", "1h", TeacherParams(), fixed_stop_pts=28.75, flat_after=11.5,
                   sessions=((2.0, 11.5),), max_trades_day=5)
    t = Run(Market.From1h(df), (b,), RiskConfig(daily_loss_limit=1e9, dd_throttle=None)).trades
    assert len(t) > 5
    assert np.allclose(t.risk_pts, 28.75)
    exit_h = pd.to_datetime(t.exit_time).dt.hour + pd.to_datetime(t.exit_time).dt.minute / 60
    entry_h = pd.to_datetime(t.entry_time).dt.hour
    assert (entry_h < 11.5).all()
    # nothing survives past the book's flat time on the entry day
    same_day = pd.to_datetime(t.exit_time).dt.date == pd.to_datetime(t.entry_time).dt.date
    assert (exit_h[same_day] <= 12.0).all()


def TestBreakevenMoveCapsLossAtScratch():
    df = Synth(n=2500, seed=9)
    b = BookConfig("t", "1h", TeacherParams(), fixed_stop_pts=30, be_at_r=0.5, max_trades_day=5)
    t = Run(Market.From1h(df), (b,), RiskConfig(flatten_eod=False, daily_loss_limit=1e9,
                                                  dd_throttle=None)).trades
    moved = t[(t.mfe_pts >= 15) & ~t.tp1_hit]
    assert len(moved)
    # BE applies from the bar after +0.5R is seen: a stop in that same bar is a full loss, and
    # a later gap below entry fills at the open. Otherwise it is an exact scratch (slippage + fees).
    scratch = moved[moved.r > -0.1]
    assert len(scratch) >= 0.6 * len(moved)
    assert np.allclose(scratch.exit_price, scratch.entry - 0.25 * scratch.side)


def TestNativeDaysRejectsUpsampledTape():
    idx = pd.date_range("2026-08-03 18:00", periods=600, freq="1min", tz=D.TZ)
    rng = np.random.default_rng(0)
    c = 20000 + np.cumsum(rng.normal(0, 2, 600))
    df = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0}, index=idx)
    fake = df.copy()
    fake.iloc[:, :4] = df.iloc[::5, :4].reindex(idx, method="ffill").to_numpy()
    assert len(D.NativeDays(df)) == 600
    assert len(D.NativeDays(fake)) == 0
