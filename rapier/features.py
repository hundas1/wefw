"""ICT context features per base bar (all computable in real time)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import data as D
from .indicators import last_confirmed, pivots


def context(base: pd.DataFrame, daily: pd.DataFrame, k_daily: int = 2) -> pd.DataFrame:
    """Per base bar:

    * ``pd_pos``   - close location inside the daily dealing range (last confirmed
      daily swing low -> swing high); <0.5 is discount, >0.5 premium.
    * ``swept_pdl`` / ``swept_pdh`` - today's session already traded through the
      prior day's low / high (a liquidity sweep).
    * ``vs_midnight`` / ``vs_dopen`` - close minus the 00:00 ET open / 18:00 session open.
    """
    td = D.trading_day(base.index)
    g = base.groupby(td)
    s_open = g["open"].transform("first").to_numpy()
    run_hi = g["high"].cummax().to_numpy()
    run_lo = g["low"].cummin().to_numpy()

    # prior completed day's high/low (daily bars are stamped at their 17:00 close)
    dh = daily["high"].shift(1)
    dl = daily["low"].shift(1)
    dkey = D.trading_day(daily.index - pd.Timedelta(hours=17))
    pdh = pd.Series(dh.to_numpy(), index=dkey).reindex(td).to_numpy()
    pdl = pd.Series(dl.to_numpy(), index=dkey).reindex(td).to_numpy()

    # midnight open: first bar at/after 00:00 ET of the trading day
    mid = base["open"].where(base.index.hour == 0)
    mid_open = mid.groupby(td).transform("first").to_numpy()
    after_mid = base.index.hour < 17
    mid_open = np.where(after_mid, mid_open, np.nan)

    # daily dealing range from confirmed daily swings, as of each base bar
    h, l = daily["high"].to_numpy(), daily["low"].to_numpy()
    ph, pl = pivots(h, l, k_daily)
    lh, ll = last_confirmed(ph, k_daily), last_confirmed(pl, k_daily)
    rng_hi = np.where(lh >= 0, h[np.maximum(lh, 0)], np.nan)
    rng_lo = np.where(ll >= 0, l[np.maximum(ll, 0)], np.nan)
    di = np.searchsorted(daily.index.asi8, base.index.asi8, side="right") - 1
    rh = np.where(di >= 0, rng_hi[np.maximum(di, 0)], np.nan)
    rl = np.where(di >= 0, rng_lo[np.maximum(di, 0)], np.nan)
    c = base["close"].to_numpy()
    lo_, hi_ = np.minimum(rh, rl), np.maximum(rh, rl)
    with np.errstate(invalid="ignore", divide="ignore"):
        pd_pos = (c - lo_) / (hi_ - lo_)

    return pd.DataFrame({
        "pd_pos": pd_pos,
        "swept_pdl": run_lo < pdl,
        "swept_pdh": run_hi > pdh,
        "vs_midnight": c - mid_open,
        "vs_dopen": c - s_open,
        "hour": base.index.hour + base.index.minute / 60,
        "dow": base.index.dayofweek,
    }, index=base.index)
