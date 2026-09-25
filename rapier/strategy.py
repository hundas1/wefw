"""Bias + OTE setup detection on any timeframe.

A *long* OTE setup at the close of bar ``i`` on timeframe T:

1. Anchor ``A`` = the most recent confirmed swing low (fractal, ``k`` bars).
2. Leg high ``H`` = highest high since the anchor bar; leg ``D = H - A``.
3. Displacement: ``H`` broke the last confirmed swing high *before* the anchor
   (break of structure) and ``D >= min_leg_atr * ATR``.
4. Anchor still intact (no low below ``A`` since the leg high).
5. Entry limit ``E = H - fib * D`` (OTE band 0.62-0.79), stop ``S = A - buf*ATR``.

Shorts mirror this. Everything uses only bars closed by ``i``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import Atr, LastConfirmed, Pivots


@dataclass(frozen=True)
class SetupParams:
    k: int = 3                 # fractal strength
    fib: float = 0.705         # OTE entry retracement
    min_leg_atr: float = 2.0   # displacement filter
    max_leg_atr: float = 12.0
    stop_buf_atr: float = 0.1
    require_bos: bool = True
    # teacher (user) impulse-shape rules, see README "Teacher OTE"
    min_leg_pts: float = 0.0     # absolute impulse size floor
    min_leg_bars: int = 0        # bars from anchor to extreme
    max_leg_bars: int = 10**9
    max_bar_frac: float = 1.0    # largest single-bar range / leg; above = spike, not a curve
    # "structure": anchor = latest confirmed swing (HTF style).
    # "impulse":   extreme = latest confirmed swing, origin = deepest opposite wick within
    #              max_leg_bars before it (the user's 1m method: measure the whole impulse).
    mode: str = "structure"


@dataclass
class Setups:
    """Per-bar arrays (NaN where no setup) for one timeframe."""
    index: pd.DatetimeIndex
    close_time: np.ndarray      # int64 ns when each bar is complete
    atr: np.ndarray
    long: dict[str, np.ndarray]
    short: dict[str, np.ndarray]
    pivot_low_price: np.ndarray  # latest confirmed swing low (for trailing)
    pivot_high_price: np.ndarray


def FindSetups(df: pd.DataFrame, tf_delta: pd.Timedelta, p: SetupParams) -> Setups:
    if p.mode == "impulse":
        return _FindImpulseSetups(df, tf_delta, p)
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(h)
    a = Atr(df)
    ph, pl = Pivots(h, l, p.k)
    lh, ll = LastConfirmed(ph, p.k), LastConfirmed(pl, p.k)

    # previous confirmed pivot high strictly before a given bar index
    ph_idx = np.flatnonzero(ph)
    pl_idx = np.flatnonzero(pl)

    def PrevPivot(idx_arr: np.ndarray, j: int) -> int:
        pos = np.searchsorted(idx_arr, j) - 1
        return int(idx_arr[pos]) if pos >= 0 else -1

    fields = ("E", "S", "A", "H", "D", "id", "x")  # x = bar index of the leg extreme
    L = {f: np.full(n, np.nan) for f in fields}
    Sh = {f: np.full(n, np.nan) for f in fields}

    for i in range(n):
        j = ll[i]
        if j >= 0:
            seg = h[j:i + 1]
            hi_rel = int(seg.argmax())
            H = seg[hi_rel]
            A = l[j]
            D = H - A
            ok = D >= p.min_leg_atr * a[i] and D <= p.max_leg_atr * a[i]
            if ok:
                ok = _ShapeOk(p, D, hi_rel, h, l, j)
            if ok and p.require_bos:
                b = PrevPivot(ph_idx, j)
                ok = b >= 0 and H > h[b]
            if ok and l[j + hi_rel:i + 1].min() >= A:
                L["E"][i] = H - p.fib * D
                L["S"][i] = A - p.stop_buf_atr * a[i]
                L["A"][i], L["H"][i], L["D"][i], L["id"][i] = A, H, D, j
                L["x"][i] = j + hi_rel
        j = lh[i]
        if j >= 0:
            seg = l[j:i + 1]
            lo_rel = int(seg.argmin())
            Lo = seg[lo_rel]
            A = h[j]
            D = A - Lo
            ok = D >= p.min_leg_atr * a[i] and D <= p.max_leg_atr * a[i]
            if ok:
                ok = _ShapeOk(p, D, lo_rel, h, l, j)
            if ok and p.require_bos:
                b = PrevPivot(pl_idx, j)
                ok = b >= 0 and Lo < l[b]
            if ok and h[j + lo_rel:i + 1].max() <= A:
                Sh["E"][i] = Lo + p.fib * D
                Sh["S"][i] = A + p.stop_buf_atr * a[i]
                Sh["A"][i], Sh["H"][i], Sh["D"][i], Sh["id"][i] = A, Lo, D, j
                Sh["x"][i] = j + lo_rel

    close_time = (df.index + tf_delta).asi8
    plp = np.where(ll >= 0, l[np.maximum(ll, 0)], np.nan)
    php = np.where(lh >= 0, h[np.maximum(lh, 0)], np.nan)
    return Setups(df.index, close_time, a, L, Sh, plp, php)


def _ShapeOk(p: SetupParams, D: float, rel: int, h: np.ndarray, l: np.ndarray, j: int) -> bool:
    if D < p.min_leg_pts or not (p.min_leg_bars <= rel <= p.max_leg_bars):
        return False
    if p.max_bar_frac < 1.0 and D > 0:
        biggest = float((h[j:j + rel + 1] - l[j:j + rel + 1]).max())
        if biggest / D > p.max_bar_frac:
            return False
    return True


def _FindImpulseSetups(df: pd.DataFrame, tf_delta: pd.Timedelta, p: SetupParams) -> Setups:
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(h)
    a = Atr(df)
    ph, pl = Pivots(h, l, p.k)
    lh, ll = LastConfirmed(ph, p.k), LastConfirmed(pl, p.k)
    W = int(min(p.max_leg_bars, 10**6))
    fields = ("E", "S", "A", "H", "D", "id", "x")
    L = {f: np.full(n, np.nan) for f in fields}
    Sh = {f: np.full(n, np.nan) for f in fields}
    for i in range(n):
        x = lh[i]  # long: the extreme is the last confirmed swing high
        if x >= 1 and h[x] >= h[x:i + 1].max():
            lo0 = max(0, x - W)
            o = lo0 + int(l[lo0:x].argmin())
            A, H = l[o], h[x]
            D = H - A
            if (p.min_leg_atr * a[i] <= D <= p.max_leg_atr * a[i] and _ShapeOk(p, D, x - o, h, l, o)
                    and l[x:i + 1].min() >= A):
                L["E"][i], L["S"][i] = H - p.fib * D, A - p.stop_buf_atr * a[i]
                L["A"][i], L["H"][i], L["D"][i], L["id"][i], L["x"][i] = A, H, D, o, x
        x = ll[i]  # short: the extreme is the last confirmed swing low
        if x >= 1 and l[x] <= l[x:i + 1].min():
            lo0 = max(0, x - W)
            o = lo0 + int(h[lo0:x].argmax())
            A, Lo = h[o], l[x]
            D = A - Lo
            if (p.min_leg_atr * a[i] <= D <= p.max_leg_atr * a[i] and _ShapeOk(p, D, x - o, h, l, o)
                    and h[x:i + 1].max() <= A):
                Sh["E"][i], Sh["S"][i] = Lo + p.fib * D, A + p.stop_buf_atr * a[i]
                Sh["A"][i], Sh["H"][i], Sh["D"][i], Sh["id"][i], Sh["x"][i] = A, Lo, D, o, x
    close_time = (df.index + tf_delta).asi8
    plp = np.where(ll >= 0, l[np.maximum(ll, 0)], np.nan)
    php = np.where(lh >= 0, h[np.maximum(lh, 0)], np.nan)
    return Setups(df.index, close_time, a, L, Sh, plp, php)
