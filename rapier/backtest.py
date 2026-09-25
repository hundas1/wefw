"""Event-driven multi-book backtester for Bias + OTE.

Execution runs on the finest available bar series (the *base clock*). Each
book reads the state of its own timeframe as of the last *completed* bar, so
higher-timeframe information is never used before it exists.

Deliberately conservative fill model:
* limit entries need a trade-through of ``fill_through_ticks``;
* when stop and target could both be hit inside one bar, the stop wins;
* on the fill bar a target only counts if the bar *closes* beyond it (which
  proves price reached it after the fill);
* stops that gap are filled at the open, plus slippage; commissions per side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from . import data as D
from .features import context
from .indicators import ema, structure_trend
from .strategy import SetupParams, Setups, find_setups

TICK = 0.25
TF_DELTA = {"1D": pd.Timedelta("1D"), "4h": pd.Timedelta("4h"), "1h": pd.Timedelta("1h"),
            "15m": pd.Timedelta("15m"), "5m": pd.Timedelta("5m"), "1m": pd.Timedelta("1m")}


@dataclass(frozen=True)
class BookConfig:
    name: str
    tf: str
    setup: SetupParams = SetupParams()
    bias_tfs: tuple[str, ...] = ("1D",)
    sessions: tuple[tuple[float, float], ...] | None = None  # ET hour windows for entries
    risk_usd: float = 400.0
    tp1_r: float = 1.0           # first target in R (always >= 1)
    partial: float = 1.0         # fraction closed at TP1 (1.0 = all out at TP1)
    runner_ext: float | None = None  # runner target = H + ext*D (long); None = no fixed target
    trail: bool = False          # trail runner stop behind confirmed TF swings
    be_after_tp1: bool = True
    be_offset_r: float = 0.0
    max_age: int = 60            # TF bars since the anchor swing
    max_hold: int | None = None  # base bars
    max_trades_day: int = 2
    min_stop_pts: float = 4.0
    max_stop_pts: float = 400.0
    # ICT context filters (evaluated on the previous completed base bar)
    pd_max: float | None = None      # longs need pd_pos <= pd_max (discount); shorts >= 1 - pd_max
    midnight: bool = False           # longs below the midnight open, shorts above
    session_open: bool = False       # longs below the 18:00 session open, shorts above
    sweep: bool = False              # longs after a prior-day-low sweep, shorts after a PDH sweep
    confirm: bool = False            # wait for a rejection close in the OTE, enter at that close
    confirm_bars: int = 3
    fixed_stop_pts: float | None = None  # stop = entry -/+ this many points instead of beyond the anchor
    be_at_r: float | None = None     # move stop to entry once price has moved this many R in favour
    flat_after: float | None = None  # ET hour after which this book is flattened / stops entering
    ema_tf: str | None = None        # golden-belt 9EMA confluence: OTE entry within ema_dist of EMA9(ema_tf)
    ema_dist: float = 10.0

    def __post_init__(self):
        if self.tp1_r < 1.0:
            raise ValueError("Rapier never plans a trade below 1R")
        if self.be_at_r is not None and self.be_at_r <= 0:
            raise ValueError("be_at_r must be positive")


@dataclass(frozen=True)
class RiskConfig:
    start_balance: float = 50_000.0
    point_value: float = 2.0       # MNQ; sizing is in micros (10 MNQ = 1 NQ)
    commission_rt: float = 1.50    # per micro, round turn, fees included
    slippage_ticks: int = 1        # on stop / market exits
    fill_through_ticks: int = 1
    max_contracts: int = 40        # 40 MNQ = 4 NQ
    daily_loss_limit: float = 900.0
    max_open: int = 2
    flatten_eod: bool = True       # prop-firm style: flat before the daily close
    flatten_time: float = 16.67    # ET hours (16:40)
    bias_k: int = 3                # fractal strength for bias structure
    dd_throttle: float | None = 1000.0  # halve risk while drawdown exceeds this
    swing_overnight: bool = False  # books named "swing-*" are exempt from the daily flatten


@dataclass
class Trade:
    book: str
    tf: str
    side: int
    entry_time: pd.Timestamp
    entry: float
    stop: float
    tp1: float
    tp2: float | None
    qty: int
    risk_pts: float
    planned_rr: float
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    pnl: float = 0.0
    r: float = 0.0
    exit_reason: str = ""
    tp1_hit: bool = False
    mfe_pts: float = 0.0
    mae_pts: float = 0.0
    # runtime state
    qty_open: int = 0
    cur_stop: float = 0.0
    bars: int = 0
    fills: list = field(default_factory=list)
    feat: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        d = asdict(self)
        for k in ("qty_open", "cur_stop", "bars", "fills", "feat"):
            d.pop(k)
        return d | self.feat


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.DataFrame  # per base bar: close-MTM, worst intrabar
    books: tuple[BookConfig, ...]
    risk: RiskConfig
    consumed: frozenset = frozenset()  # "book:side:anchor" limits already traded into


class Market:
    """Bars for every timeframe plus cached setups/bias to make sweeps cheap."""

    def __init__(self, base: pd.DataFrame, frames: dict[str, pd.DataFrame]):
        frames = {k: _ns(v) for k, v in frames.items()}
        base = _ns(base)
        self.base = base
        self.frames = frames
        self._setups: dict = {}
        self._trend: dict = {}
        self._ctx = None

    def context(self) -> pd.DataFrame:
        if self._ctx is None:
            self._ctx = context(self.base, self.frames["1D"])
        return self._ctx

    @classmethod
    def from_1h(cls, h1: pd.DataFrame, lower: dict[str, pd.DataFrame] | None = None,
                base_tf: str = "1h") -> "Market":
        frames = {"1h": h1, "4h": D.resample(h1, "4h"), "1D": D.resample(h1, "1D")}
        frames.update(lower or {})
        return cls(frames[base_tf], frames)

    def setups(self, tf: str, p: SetupParams) -> Setups:
        key = (tf, p)
        if key not in self._setups:
            self._setups[key] = find_setups(self.frames[tf], TF_DELTA[tf] if tf != "1D" else pd.Timedelta(0), p)
        return self._setups[key]

    def trend(self, tf: str, k: int) -> tuple[np.ndarray, np.ndarray]:
        key = (tf, k)
        if key not in self._trend:
            f = self.frames[tf]
            ct = f.index.asi8 if tf == "1D" else (f.index + TF_DELTA[tf]).asi8
            self._trend[key] = (ct, structure_trend(f, k))
        return self._trend[key]


def _asof(close_times: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Index of the latest bar complete at or before each time in ``t`` (or -1)."""
    return np.searchsorted(close_times, t, side="right") - 1


def _ns(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.unit != "ns":
        df = df.copy()
        df.index = df.index.as_unit("ns")
    return df


def run(mkt: Market, books: tuple[BookConfig, ...], risk: RiskConfig,
        start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None,
        record_features: bool = False, close_at_end: bool = True) -> Result:
    base = mkt.base
    cx_all = mkt.context()
    if start is not None:
        base = base[base.index >= pd.Timestamp(start, tz=D.TZ)]
    if end is not None:
        base = base[base.index < pd.Timestamp(end, tz=D.TZ) + pd.Timedelta("1D")]
    # context as of the previous completed bar (no peeking at the fill bar)
    feat = cx_all.shift(1).reindex(base.index)
    f_pd, f_mid, f_dop = (feat[x].to_numpy(dtype=float) for x in ("pd_pos", "vs_midnight", "vs_dopen"))
    f_spl = feat["swept_pdl"].fillna(False).to_numpy(dtype=bool)
    f_sph = feat["swept_pdh"].fillna(False).to_numpy(dtype=bool)
    t_ns = base.index.asi8
    o, h, l, c = (base[x].to_numpy() for x in ("open", "high", "low", "close"))
    base_delta = pd.Timedelta(mkt.base.index.to_series().diff().mode().iloc[0])
    hours = (base.index.hour + base.index.minute / 60).to_numpy()
    end_hours = hours + base_delta / pd.Timedelta("1h")
    tday = D.trading_day(base.index).asi8
    n = len(base)

    pv, slip = risk.point_value, risk.slippage_ticks * TICK
    thru = risk.fill_through_ticks * TICK

    ctx = []
    trend_feats = {}
    if record_features:
        for btf in ("1D", "4h", "1h"):
            ct, tr = mkt.trend(btf, risk.bias_k)
            bi = _asof(ct, t_ns)
            trend_feats[f"bias_{btf}"] = np.where(bi >= 0, tr[np.maximum(bi, 0)], 0)
    for b in books:
        s = mkt.setups(b.tf, b.setup)
        tf_idx = _asof(s.close_time, t_ns)
        bias = np.ones(n, int) * 2  # 2 = both directions allowed
        for btf in b.bias_tfs:
            ct, tr = mkt.trend(btf, risk.bias_k)
            bi = _asof(ct, t_ns)
            trv = np.where(bi >= 0, tr[np.maximum(bi, 0)], 0)
            bias = np.where(bias == 2, trv, np.where(bias == trv, bias, 0))
        if b.sessions:
            sess = np.zeros(n, bool)
            for a, z in b.sessions:
                sess |= (hours >= a) & (hours < z)
        else:
            sess = np.ones(n, bool)
        if b.flat_after is not None:
            sess &= ~((hours >= b.flat_after) & (hours < 17.5))
        ema_at = None
        if b.ema_tf:
            ef = mkt.frames[b.ema_tf]
            ev = ema(ef["close"].to_numpy(), 9)
            ei = _asof((ef.index + TF_DELTA[b.ema_tf]).asi8, t_ns)
            ema_at = np.where(ei >= 0, ev[np.maximum(ei, 0)], np.nan)
        ctx.append(dict(cfg=b, s=s, tf_idx=tf_idx, bias=bias, sess=sess, ema=ema_at))

    realized = 0.0
    peak = 0.0
    open_pos: dict[int, Trade] = {}
    consumed: set = set()
    trades: list[Trade] = []
    day_pnl: dict[int, float] = {}
    day_count: dict[tuple, int] = {}
    pending: dict[int, dict] = {}
    eq_close = np.zeros(n)
    eq_low = np.zeros(n)

    def close_qty(tr: Trade, qty: int, px: float, when, reason: str):
        nonlocal realized
        pnl = tr.side * (px - tr.entry) * qty * pv - risk.commission_rt * qty
        tr.pnl += pnl
        tr.qty_open -= qty
        tr.fills.append((when, px, qty, reason))
        realized += pnl
        day_pnl[td] = day_pnl.get(td, 0.0) + pnl
        if tr.qty_open == 0:
            tr.exit_time = when
            tr.exit_price = sum(f[1] * f[2] for f in tr.fills) / sum(f[2] for f in tr.fills)
            tr.exit_reason = reason
            tr.r = tr.pnl / (tr.risk_pts * tr.qty * pv)

    for i in range(n):
        td = int(tday[i])
        when = base.index[i]
        flat_bar = risk.flatten_eod and end_hours[i] > risk.flatten_time and hours[i] < 17.5

        # ---- manage open positions
        for bi_, tr in list(open_pos.items()):
            cfg = ctx[bi_]["cfg"]
            tr.bars += 1
            if flat_bar and not (risk.swing_overnight and cfg.name.startswith("swing")):
                close_qty(tr, tr.qty_open, o[i] - tr.side * slip, when, "eod")
                del open_pos[bi_]
                continue
            if cfg.flat_after is not None and cfg.flat_after <= hours[i] < 17.5:
                close_qty(tr, tr.qty_open, o[i] - tr.side * slip, when, "flat")
                del open_pos[bi_]
                continue
            adverse = l[i] if tr.side > 0 else h[i]
            favor = h[i] if tr.side > 0 else l[i]
            tr.mfe_pts = max(tr.mfe_pts, tr.side * (favor - tr.entry))
            tr.mae_pts = max(tr.mae_pts, tr.side * (tr.entry - adverse))
            if tr.side * (adverse - tr.cur_stop) <= 0:
                gap = tr.side * (o[i] - tr.cur_stop) < 0
                px = (o[i] if gap else tr.cur_stop) - tr.side * slip
                close_qty(tr, tr.qty_open, px, when, "tp1+stop" if tr.tp1_hit else "stop")
                del open_pos[bi_]
                continue
            _targets(tr, cfg, favor, when, close_qty)
            if tr.qty_open == 0:
                del open_pos[bi_]
                continue
            if cfg.be_at_r and not tr.tp1_hit and tr.mfe_pts >= cfg.be_at_r * tr.risk_pts \
                    and tr.side * (tr.entry - tr.cur_stop) > 0:
                tr.cur_stop = tr.entry  # applies from the next bar on
            if cfg.trail and tr.tp1_hit:
                s = ctx[bi_]["s"]
                k = ctx[bi_]["tf_idx"][i]
                if k >= 0:
                    piv = s.pivot_low_price[k] if tr.side > 0 else s.pivot_high_price[k]
                    if not math.isnan(piv):
                        cand = piv - tr.side * cfg.setup.stop_buf_atr * s.atr[k]
                        if tr.side * (cand - tr.cur_stop) > 0 and tr.side * (c[i] - cand) > 0:
                            tr.cur_stop = cand
            if cfg.max_hold and tr.bars >= cfg.max_hold:
                close_qty(tr, tr.qty_open, c[i] - tr.side * slip, when, "time")
                del open_pos[bi_]

        # ---- new entries
        dd_now = peak - realized
        can_trade = (not flat_bar and day_pnl.get(td, 0.0) > -risk.daily_loss_limit
                     and not (risk.flatten_eod and 16.0 <= hours[i] < 18.0))
        for bi_, cx in enumerate(ctx):
            if bi_ in open_pos or not can_trade or len(open_pos) >= risk.max_open:
                pending.pop(bi_, None)
                continue
            cfg, s = cx["cfg"], cx["s"]
            if cfg.confirm and bi_ in pending:
                pd_ = pending[bi_]
                side, stop = pd_["side"], pd_["stop"]
                if i > pd_["until"] or side * (l[i] if side > 0 else h[i]) <= side * stop:
                    pending.pop(bi_)
                elif side * (c[i] - o[i]) > 0 and side * (c[i] - pd_["E"]) > 0:
                    pending.pop(bi_)
                    entry = c[i] + side * slip
                    if pd_["cfg"].fixed_stop_pts:
                        stop = entry - side * pd_["cfg"].fixed_stop_pts
                    _open(pd_["cfg"], bi_, side, entry, stop, pd_["H"], pd_["D"], when, i,
                          pd_["feat"], trades, open_pos, day_count, td, risk, dd_now, close_qty,
                          c, l, h, confirm_bar=True)
                continue
            k = cx["tf_idx"][i]
            if k < 0:
                continue
            for side, S in ((1, s.long), (-1, s.short)):
                E = S["E"][k]
                if math.isnan(E):
                    continue
                anchor = int(S["id"][k])
                key = (bi_, side, anchor)
                if key in consumed or k - anchor > cfg.max_age:
                    continue
                touched = (l[i] <= E - thru) if side > 0 else (h[i] >= E + thru)
                if not touched:
                    continue
                consumed.add(key)
                bias = cx["bias"][i]
                if not cx["sess"][i] or not (bias == 2 or bias == side):
                    continue
                if day_count.get((td, bi_), 0) >= cfg.max_trades_day:
                    continue
                if not _context_ok(cfg, side, i, f_pd, f_mid, f_dop, f_spl, f_sph):
                    continue
                if cx["ema"] is not None:
                    ev = cx["ema"][i]
                    if math.isnan(ev) or abs(E - ev) > cfg.ema_dist:
                        continue
                fill = min(o[i], E) if side > 0 else max(o[i], E)
                stop = S["S"][k]
                if cfg.fixed_stop_pts:
                    stop = fill - side * cfg.fixed_stop_pts
                f = {}
                if record_features:
                    f = {kk: int(v[i]) for kk, v in trend_feats.items()} | {
                        "pd_pos": f_pd[i], "vs_midnight": f_mid[i], "vs_dopen": f_dop[i],
                        "swept_pdl": bool(f_spl[i]), "swept_pdh": bool(f_sph[i]),
                        "hour": hours[i], "dow": int(base.index[i].dayofweek),
                        "leg_atr": S["D"][k] / s.atr[k], "age": k - anchor,
                    }
                if cfg.confirm:
                    pending[bi_] = dict(cfg=cfg, side=side, E=E, stop=stop, H=S["H"][k], D=S["D"][k],
                                        until=i + cfg.confirm_bars, feat=f)
                    break
                _open(cfg, bi_, side, fill, stop, S["H"][k], S["D"][k], when, i, f, trades,
                      open_pos, day_count, td, risk, dd_now, close_qty, c, l, h)
                break

        # ---- equity marks
        u_close = sum(t.side * (c[i] - t.entry) * t.qty_open * pv for t in open_pos.values())
        u_low = sum(t.side * ((l[i] if t.side > 0 else h[i]) - t.entry) * t.qty_open * pv
                    for t in open_pos.values())
        eq_close[i] = realized + u_close
        eq_low[i] = realized + u_low
        peak = max(peak, eq_close[i], realized)

    # force-close anything still open at the end of data (live mode keeps them open)
    if close_at_end:
        for bi_, tr in list(open_pos.items()):
            td = int(tday[-1])
            close_qty(tr, tr.qty_open, c[-1], base.index[-1], "end")

    tdf = pd.DataFrame([t.to_row() for t in trades])
    eq = pd.DataFrame({"equity": eq_close, "equity_low": eq_low}, index=base.index)
    names = frozenset(f"{books[b].name}:{s}:{a}" for b, s, a in consumed)
    return Result(tdf, eq, tuple(books), risk, names)


def _open(cfg, bi_, side, fill, stop, H, Dleg, when, i, feat, trades, open_pos, day_count, td,
          risk: RiskConfig, dd_now, close_qty, c, l, h, confirm_bar=False) -> None:
    pv, slip = risk.point_value, risk.slippage_ticks * TICK
    risk_pts = side * (fill - stop)
    if not (cfg.min_stop_pts <= risk_pts <= cfg.max_stop_pts):
        return
    budget = cfg.risk_usd * (0.5 if risk.dd_throttle and dd_now > risk.dd_throttle else 1.0)
    qty = min(risk.max_contracts, int(budget // (risk_pts * pv)))
    if qty < 1:
        return
    tp1 = fill + side * cfg.tp1_r * risk_pts
    tp2 = None
    if cfg.partial < 1.0 and cfg.runner_ext is not None:
        tp2 = H + side * cfg.runner_ext * Dleg
        if side * (tp2 - tp1) <= 0:
            tp2 = tp1 + side * risk_pts
    planned = (side * ((tp2 if tp2 is not None else tp1) - fill)) / risk_pts
    tr = Trade(cfg.name, cfg.tf, side, when, fill, stop, tp1, tp2, qty, risk_pts,
               round(planned, 2), qty_open=qty, cur_stop=stop, feat=dict(feat))
    trades.append(tr)
    day_count[(td, bi_)] = day_count.get((td, bi_), 0) + 1
    if confirm_bar:
        # entered at the bar close: nothing left of this bar to resolve
        open_pos[bi_] = tr
        return
    # same-bar resolution: stop first; targets only if the close proves them
    adverse = l[i] if side > 0 else h[i]
    if side * (adverse - stop) <= 0:
        close_qty(tr, qty, stop - side * slip, when, "stop")
    else:
        _targets(tr, cfg, c[i], when, close_qty)
        if tr.qty_open:
            open_pos[bi_] = tr


def _context_ok(cfg: BookConfig, side: int, i: int, f_pd, f_mid, f_dop, f_spl, f_sph) -> bool:
    if cfg.pd_max is not None:
        v = f_pd[i]
        if math.isnan(v) or (side > 0 and v > cfg.pd_max) or (side < 0 and v < 1 - cfg.pd_max):
            return False
    if cfg.midnight:
        v = f_mid[i]
        if math.isnan(v) or side * v > 0:
            return False
    if cfg.session_open and side * f_dop[i] > 0:
        return False
    if cfg.sweep and not (f_spl[i] if side > 0 else f_sph[i]):
        return False
    return True


def _targets(tr: Trade, cfg: BookConfig, favor: float, when, close_qty) -> None:
    side = tr.side
    if not tr.tp1_hit and side * (favor - tr.tp1) >= 0:
        tr.tp1_hit = True
        q = tr.qty_open if cfg.partial >= 1.0 or tr.tp2 is None and not cfg.trail else max(1, round(tr.qty * cfg.partial))
        q = min(q, tr.qty_open)
        close_qty(tr, q, tr.tp1, when, "tp1")
        if tr.qty_open and cfg.be_after_tp1:
            tr.cur_stop = tr.entry + side * cfg.be_offset_r * tr.risk_pts
    if tr.qty_open and tr.tp1_hit and tr.tp2 is not None and side * (favor - tr.tp2) >= 0:
        close_qty(tr, tr.qty_open, tr.tp2, when, "tp2")
