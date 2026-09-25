"""Market data: fetch, cache, clean, roll-adjust and resample NQ bars.

Yahoo's ``NQ=F`` is an *unadjusted* continuous contract. It switches to the
next quarterly contract roughly a week before expiry, and during the switch
some hourly bars mix prices from both contracts (±~250 pt flips). Left alone
that creates fake swings and fake P&L, so :func:`roll_adjust` detects each
switch, drops the contaminated bars and back-adjusts older history.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "America/New_York"
SYMBOL = "NQ=F"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COLS = ["open", "high", "low", "close", "volume"]

# Yahoo intraday history limits (per request).
_PERIODS = {"1h": "730d", "15m": "60d", "5m": "60d", "1m": "8d", "1d": "5y"}


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[COLS].astype(float)
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    # one resolution everywhere: the engine compares raw int64 timestamps across frames
    df.index = idx.tz_convert(TZ).as_unit("ns")
    df.index.name = "time"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["open", "high", "low", "close"])


def cache_path(interval: str, symbol: str = SYMBOL) -> Path:
    return DATA_DIR / f"{symbol.replace('=', '_')}_{interval}.csv.gz"


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load OHLCV from a CSV with a time column (e.g. a Databento/TradingView export)."""
    df = pd.read_csv(path)
    tcol = next(c for c in df.columns if c.lower() in ("time", "datetime", "date", "timestamp", "ts_event"))
    df = df.set_index(pd.to_datetime(df[tcol], utc=True)).drop(columns=[tcol])
    if "volume" not in {c.lower() for c in df.columns}:
        df["volume"] = 0.0
    return _normalize(df)


def fetch(interval: str, symbol: str = SYMBOL, refresh: bool = True) -> pd.DataFrame:
    """Fetch bars from Yahoo and merge them into an append-only local cache.

    The cache only grows, so running ``rapier fetch`` regularly accumulates
    5m/15m history beyond Yahoo's 60-day window.
    """
    path = cache_path(interval, symbol)
    cached = load_csv(path) if path.exists() else None
    if refresh or cached is None:
        import yfinance as yf

        new = yf.download(symbol, period=_PERIODS[interval], interval=interval,
                          progress=False, auto_adjust=False)
        if len(new):
            new = _normalize(new)
            merged = new if cached is None else pd.concat([cached, new])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            path.parent.mkdir(parents=True, exist_ok=True)
            merged.to_csv(path, compression="gzip", index_label="time")
            cached = merged
    if cached is None:
        raise RuntimeError(f"No data for {symbol} {interval}")
    return cached


def native_days(df: pd.DataFrame, max_repeat: float = 0.1) -> pd.DataFrame:
    """Keep only trading days whose bars are genuinely at this resolution.

    Some saved "1m" tapes are coarser bars forward-filled onto a 1m grid (every
    bar repeated 2-5 times). Such days fake intrabar detail, so any day where
    more than ``max_repeat`` of the rows exactly repeat the previous OHLC is dropped.
    """
    rep = df[["open", "high", "low", "close"]].diff().abs().sum(axis=1).eq(0)
    td = trading_day(df.index)
    share = rep.groupby(td).mean()
    good = share[share <= max_repeat].index
    return df[td.isin(good)]


def import_tape(path: str | Path, interval: str, symbol: str = SYMBOL) -> tuple[int, int]:
    """Merge an external OHLCV CSV into the local cache (validated, native days only).

    Existing cached bars win on overlap. Returns (rows added, days rejected).
    """
    new = load_csv(path)
    step = new.index.to_series().diff().mode().iloc[0]
    if step != pd.Timedelta(interval):
        raise ValueError(f"{path}: bar spacing {step} does not match {interval}")
    kept = native_days(new)
    rejected = trading_day(new.index).nunique() - trading_day(kept.index).nunique()
    p = cache_path(interval, symbol)
    cached = load_csv(p) if p.exists() else kept.iloc[:0]
    merged = pd.concat([kept, cached])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    p.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(p, compression="gzip", index_label="time")
    return len(merged) - len(cached), rejected


# --------------------------------------------------------------------------- rolls

def third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def quarterly_expiries(start: pd.Timestamp, end: pd.Timestamp) -> list[dt.date]:
    out = []
    for y in range(start.year, end.year + 1):
        for m in (3, 6, 9, 12):
            e = third_friday(y, m)
            if start.date() - dt.timedelta(days=14) <= e <= end.date() + dt.timedelta(days=14):
                out.append(e)
    return out


@dataclass
class Roll:
    expiry: dt.date
    switch: pd.Timestamp
    spread: float
    dropped: int
    method: str
    drop_from: pd.Timestamp | None = None  # contaminated bars span [drop_from, switch)


def detect_rolls(df: pd.DataFrame, carry: float = 0.0095) -> list[Roll]:
    """Find the bar where ``NQ=F`` switches contract before each quarterly expiry.

    Every switch verified against the dated NQZ26 contract happened inside
    expiry week (Sunday open, or a Tue/Wed flip-flop). In that window we look
    for open-vs-previous-close jumps near the expected calendar spread
    (``carry`` x price; NQ is in contango so the new contract is higher).
    Flip-flop runs (+S, -S, +S ...) collapse to the last +S jump; bars between
    the first and last flip are contaminated and dropped. With no clean jump the
    switch is hidden in the weekend gap: use the Sunday open and theoretical carry.
    """
    rolls = []
    jump = df["open"] - df["close"].shift()
    gap_open = df.index.to_series().diff() > pd.Timedelta("90min")
    for exp in quarterly_expiries(df.index[0], df.index[-1]):
        lo = pd.Timestamp(exp - dt.timedelta(days=5), tz=TZ) + pd.Timedelta(hours=12)
        hi = pd.Timestamp(exp, tz=TZ)
        w = jump.loc[lo:hi]
        if w.empty or df.index[-1] < hi or df.index[0] > lo:
            continue
        s0 = carry * float(df["close"].loc[lo:hi].median())
        intra = w[~gap_open.loc[w.index]]
        cand = intra[(intra.abs() > 0.6 * s0) & (intra.abs() < 1.6 * s0)]
        pos = cand[cand > 0]
        if len(pos) and len(cand) >= 2:
            first, last = cand.index[0], pos.index[-1]
            inner = df.loc[first:last].index
            # the final +S bar opens on the new contract; bars from the first
            # flip up to it are contaminated
            rolls.append(Roll(exp, last, float(pos.median()), len(inner) - 1, "flip", inner[0]))
        else:
            opens = w[gap_open.loc[w.index]]
            if opens.empty:
                continue
            rolls.append(Roll(exp, opens.index[0], s0, 0, "sunday-open"))
    for r in rolls:
        r.spread = round(r.spread * 4) / 4  # keep prices on the 0.25 tick grid
    return rolls


def roll_adjust(df: pd.DataFrame, rolls: list[Roll] | None = None) -> tuple[pd.DataFrame, list[Roll]]:
    """Back-adjust prices (additively) so history is continuous with the latest contract.

    Pass ``rolls`` detected on 1h data to adjust other timeframes consistently.
    """
    rolls = detect_rolls(df) if rolls is None else rolls
    out = df.copy()
    drop = np.zeros(len(out), bool)
    for r in rolls:
        if r.drop_from is not None:
            drop |= (out.index >= r.drop_from) & (out.index < r.switch)
    for r in rolls:
        mask = out.index < r.switch
        out.loc[mask, ["open", "high", "low", "close"]] += r.spread
    return out[~drop], rolls


# ---------------------------------------------------------------------- resampling

def trading_day(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """CME trading date: the session that opens 18:00 ET belongs to the next day."""
    return (index + pd.Timedelta(hours=6)).normalize().tz_localize(None)


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample to ``4h`` (session-aligned: 18,22,02,06,10,14 ET) or ``1D`` (trading day)."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if rule.upper() in ("1D", "D"):
        g = df.groupby(trading_day(df.index)).agg(agg)
        g.index = pd.DatetimeIndex(g.index).tz_localize(TZ) + pd.Timedelta(hours=17)
        g.index.name = "time"
        # stamp daily bars at their *close* (17:00 ET) so consumers can't peek early
        return g.dropna()
    out = df.resample(rule, origin="start_day", offset="2h", label="left", closed="left").agg(agg)
    return out.dropna()


def bar_close_times(df: pd.DataFrame, rule: str) -> pd.Series:
    """Time at which each bar is complete (used to avoid look-ahead across timeframes)."""
    if rule.upper() in ("1D", "D"):
        return pd.Series(df.index, index=df.index)
    return pd.Series(df.index + pd.Timedelta(rule), index=df.index)


def load_nq(intervals=("1h",), refresh: bool = True) -> dict[str, pd.DataFrame]:
    """Fetch, clean and roll-adjust NQ bars for each requested interval."""
    out = {}
    base, rolls = roll_adjust(fetch("1h", refresh=refresh))
    out["1h"] = base
    out["rolls"] = rolls
    for iv in intervals:
        if iv == "1h":
            continue
        if iv == "15m" and cache_path("5m").exists():
            # 5m history reaches further back (imported tapes); 15m = resampled 5m
            out[iv] = resample(roll_adjust(fetch("5m", refresh=refresh), rolls)[0], "15min")
            continue
        out[iv] = roll_adjust(fetch(iv, refresh=refresh), rolls)[0]
    return out


def splice(long_h1: pd.DataFrame, recent: pd.DataFrame) -> pd.DataFrame:
    """Prepend older 1h history to a (more accurate) recent 1h series.

    The older series is shifted by the median close difference over the overlap,
    so the two back-adjusted series line up at the join.
    """
    if long_h1.empty:
        return recent
    ov = recent["close"].reindex(long_h1.index).dropna()
    shift = float((ov - long_h1["close"].reindex(ov.index)).median()) if len(ov) else 0.0
    older = long_h1[long_h1.index < recent.index[0]].copy()
    older[["open", "high", "low", "close"]] += round(shift * 4) / 4
    return pd.concat([older, recent]).sort_index()


def frames_from_1m(m1: pd.DataFrame, long_h1: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Build every timeframe from one continuous 1m series (e.g. IBKR).

    ``long_h1`` (Yahoo, roll-adjusted) only supplies history *before* the 1m
    series starts, so higher-timeframe bias has enough warm-up.
    """
    h1 = resample(m1, "1h")
    if long_h1 is not None:
        h1 = splice(long_h1, h1)
    return {"1m": m1, "5m": resample(m1, "5min"), "15m": resample(m1, "15min"), "1h": h1}



__all__ = [
    "fetch", "load_csv", "load_nq", "roll_adjust", "detect_rolls", "resample",
    "trading_day", "bar_close_times", "Roll",
]
