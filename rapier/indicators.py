"""Look-ahead-free indicators and market structure."""

from __future__ import annotations

import numpy as np
import pandas as pd


def Atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def Ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def Pivots(high: np.ndarray, low: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Fractal swing points with ``k`` bars each side.

    Returns boolean arrays marking the *pivot bar*. A pivot at ``j`` is only
    known at the close of bar ``j + k``; callers must respect that delay.
    """
    n = len(high)
    ph = np.zeros(n, bool)
    pl = np.zeros(n, bool)
    for j in range(k, n - k):
        hw = high[j - k:j + k + 1]
        lw = low[j - k:j + k + 1]
        if high[j] == hw.max() and (hw[:k] < high[j]).all():
            ph[j] = True
        if low[j] == lw.min() and (lw[:k] > low[j]).all():
            pl[j] = True
    return ph, pl


def LastConfirmed(flags: np.ndarray, k: int) -> np.ndarray:
    """For each bar i, index of the latest pivot confirmed by the close of i (or -1)."""
    n = len(flags)
    out = np.full(n, -1)
    last = -1
    for i in range(n):
        j = i - k
        if j >= 0 and flags[j]:
            last = j
        out[i] = last
    return out


def StructureTrend(df: pd.DataFrame, k: int) -> np.ndarray:
    """Market-structure bias per bar: +1 after a close above the last confirmed
    swing high (bullish BOS), -1 after a close below the last swing low."""
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    ph, pl = Pivots(h, l, k)
    lh, ll = LastConfirmed(ph, k), LastConfirmed(pl, k)
    out = np.zeros(len(c), int)
    state = 0
    for i in range(len(c)):
        if lh[i] >= 0 and c[i] > h[lh[i]]:
            state = 1
        elif ll[i] >= 0 and c[i] < l[ll[i]]:
            state = -1
        out[i] = state
    return out
