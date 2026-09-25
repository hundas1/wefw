"""The Prop Firm Rapier system: which books run, and how, from one params dict."""

from __future__ import annotations

import json
from pathlib import Path

from .backtest import BookConfig, RiskConfig
from .strategy import SetupParams

CONFIG_PATH = Path(__file__).resolve().parent / "rapier_config.json"

SESSIONS = {
    "any": None,
    "ny": ((8.0, 16.0),),
    "ny_am": ((9.5, 12.0),),
    "ny_open2h": ((9.5, 11.5),),   # 06:30-08:30 PT, the user's morning book
    "lon_ny": ((2.0, 5.0), (8.0, 16.0)),
}

# Hand-picked starting point before optimisation.
DEFAULT_PARAMS: dict = {
    "swing": {"enabled": True, "tf": "4h", "k": 2, "fib": 0.62, "min_leg": 1.5, "bias": ["1D", "4h", "1h"],
              "confirm": False, "risk": 400, "partial": 0.5, "runner_ext": 2.0, "trail": True,
              "sessions": "any", "pd_max": None, "midnight": False, "sweep": False, "max_hold": 360},
    "intraday": {"enabled": True, "tf": "1h", "k": 2, "fib": 0.705, "min_leg": 1.5, "bias": ["1D"],
                 "confirm": True, "risk": 300, "tp1_r": 1.0, "sessions": "ny", "pd_max": None,
                 "midnight": False, "sweep": False},
    "scalp": {"enabled": False, "tfs": ["15m", "5m"], "k": 2, "fib": 0.705, "min_leg": 2.0,
              "bias": ["1D", "1h"], "confirm": False, "risk": 250, "tp1_r": 1.0, "sessions": "ny_am"},
    # The user's 1m OTE ("teacher" rules from the original Bee Sid bot).
    "teacher": {"enabled": False, "tf": "1m", "fib": 0.705, "min_leg_pts": 55.0, "min_leg_bars": 12,
                "max_leg_bars": 36, "max_bar_frac": 0.52, "stop": "swing", "fixed_stop_pts": 28.75,
                "be_at_r": None, "bias": [], "risk": 250, "tp1_r": 1.0, "sessions": "ny_open2h"},
    "risk": {"daily_loss_limit": 800, "max_open": 2, "flatten_eod": True, "swing_overnight": True,
             "max_contracts": 40, "dd_throttle": 1000},
}


def load_params(path: Path | str | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    return json.loads(p.read_text())["params"] if p.exists() else DEFAULT_PARAMS


def build(params: dict, include_scalp: bool | None = None,
          include_teacher: bool | None = None) -> tuple[tuple[BookConfig, ...], RiskConfig]:
    """Turn a params dict into book configs.

    The swing book may hold overnight (``swing_overnight``) while every other
    book is flattened before the close; that is modelled with ``max_hold``
    on the swing book and the global ``flatten_eod`` switch.
    ``include_scalp`` / ``include_teacher`` override the enabled flags (the
    lower-timeframe books need a 5m / 1m execution clock).
    """
    books = []
    r = params["risk"]
    sw = params["swing"]
    if sw.get("enabled"):
        books.append(BookConfig(
            name=f"swing-{sw['tf']}", tf=sw["tf"],
            setup=SetupParams(k=sw["k"], fib=sw["fib"], min_leg_atr=sw["min_leg"]),
            bias_tfs=tuple(sw["bias"]), sessions=SESSIONS[sw["sessions"]], risk_usd=sw["risk"],
            tp1_r=1.0, partial=sw["partial"], runner_ext=sw["runner_ext"], trail=sw["trail"],
            confirm=sw["confirm"], pd_max=sw["pd_max"], midnight=sw["midnight"], sweep=sw["sweep"],
            max_hold=sw.get("max_hold"), max_trades_day=1, max_age=60))
    it = params["intraday"]
    if it.get("enabled"):
        books.append(BookConfig(
            name=f"ote-{it['tf']}", tf=it["tf"],
            setup=SetupParams(k=it["k"], fib=it["fib"], min_leg_atr=it["min_leg"]),
            bias_tfs=tuple(it["bias"]), sessions=SESSIONS[it["sessions"]], risk_usd=it["risk"],
            tp1_r=it["tp1_r"], confirm=it["confirm"], pd_max=it["pd_max"], midnight=it["midnight"],
            sweep=it["sweep"], max_trades_day=2, max_age=120))
    sc = params["scalp"]
    if (sc.get("enabled") if include_scalp is None else include_scalp):
        for tf in sc["tfs"]:
            books.append(BookConfig(
                name=f"scalp-{tf}", tf=tf,
                setup=SetupParams(k=sc["k"], fib=sc["fib"], min_leg_atr=sc["min_leg"]),
                bias_tfs=tuple(sc["bias"]), sessions=SESSIONS[sc["sessions"]], risk_usd=sc["risk"],
                tp1_r=sc["tp1_r"], confirm=sc["confirm"], max_trades_day=2, max_age=150,
                min_stop_pts=3.0, max_stop_pts=80.0))
    te = params.get("teacher", DEFAULT_PARAMS["teacher"])
    if (te.get("enabled") if include_teacher is None else include_teacher):
        fl = SESSIONS[te["sessions"]]
        books.append(BookConfig(
            name=f"teacher-{te['tf']}", tf=te["tf"],
            setup=SetupParams(k=2, fib=te["fib"], min_leg_atr=0.0, max_leg_atr=1e9,
                              min_leg_pts=te["min_leg_pts"], min_leg_bars=te["min_leg_bars"],
                              max_leg_bars=te["max_leg_bars"], max_bar_frac=te["max_bar_frac"],
                              require_bos=False, mode="impulse"),
            bias_tfs=tuple(te["bias"]), sessions=fl, flat_after=max(z for _, z in fl),
            risk_usd=te["risk"], tp1_r=te["tp1_r"], be_at_r=te["be_at_r"],
            fixed_stop_pts=te["fixed_stop_pts"] if te["stop"] == "fixed" else None,
            max_trades_day=3, max_age=90, min_stop_pts=3.0, max_stop_pts=80.0))
    risk = RiskConfig(daily_loss_limit=r["daily_loss_limit"], max_open=r["max_open"],
                      flatten_eod=r["flatten_eod"], max_contracts=r["max_contracts"],
                      dd_throttle=r["dd_throttle"], swing_overnight=r.get("swing_overnight", False))
    return tuple(books), risk
