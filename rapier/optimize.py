"""Random-search refinement loop with an out-of-sample guard.

Each candidate is scored on the requested window (in-sample, IS) *and* on the
preceding year (out-of-sample, OOS). A candidate only counts if drawdown stays
under $2,000 in both; it is ranked by the *worse* of its two win rates so a
config that merely memorised one period can't win.
"""

from __future__ import annotations

import copy
import json
import os
import random
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from . import data as D
from .backtest import Market, run
from .metrics import GOALS, summarize
from .system import CONFIG_PATH, DEFAULT_PARAMS, build

IS_START, OOS_START, OOS_END = "2025-07-26", "2024-06-15", "2025-07-25"

SPACE = {
    "swing": {"enabled": [True, True, False], "tf": ["4h", "1h"], "k": [2, 3], "fib": [0.62, 0.705, 0.79],
              "min_leg": [1.5, 2.5], "bias": [["1D"], ["1D", "4h"], ["1D", "4h", "1h"]],
              "confirm": [False, True], "risk": [250, 400, 600], "partial": [0.34, 0.5, 0.67],
              "runner_ext": [1.0, 2.0, 3.0, None], "trail": [True, False], "sessions": ["any", "ny", "lon_ny"],
              "pd_max": [None, 0.5], "midnight": [False, True], "sweep": [False], "max_hold": [120, 360, None]},
    "intraday": {"enabled": [True, True, False], "tf": ["1h"], "k": [2, 3], "fib": [0.62, 0.705, 0.79],
                 "min_leg": [1.5, 2.5], "bias": [["1D"], ["1D", "4h"], ["1D", "4h", "1h"]],
                 "confirm": [True, False], "risk": [200, 300, 400], "tp1_r": [1.0, 1.25],
                 "sessions": ["ny", "ny_am", "any", "lon_ny"], "pd_max": [None, 0.5],
                 "midnight": [False, True], "sweep": [False, True]},
    "risk": {"daily_loss_limit": [600, 900], "max_open": [1, 2], "flatten_eod": [True],
             "swing_overnight": [True, False], "max_contracts": [40], "dd_throttle": [None, 800]},
}

_MKT: Market | None = None


def _init():
    global _MKT
    h1, _ = D.roll_adjust(D.fetch("1h", refresh=False))
    _MKT = Market.from_1h(h1)


def sample(rng: random.Random) -> dict:
    p = copy.deepcopy(DEFAULT_PARAMS)
    for book, space in SPACE.items():
        for k, choices in space.items():
            p[book][k] = rng.choice(choices)
    if not (p["swing"]["enabled"] or p["intraday"]["enabled"]):
        p["intraday"]["enabled"] = True
    return p


def evaluate(params: dict) -> dict:
    books, risk = build(params, include_scalp=False)
    out = {"params": params}
    for tag, (a, b) in {"is": (IS_START, None), "oos": (OOS_START, OOS_END)}.items():
        out[tag] = summarize(run(_MKT, books, risk, start=a, end=b))
    out["score"] = score(out["is"], out["oos"])
    return out


def score(i: dict, o: dict, min_trades: int = 20) -> float:
    if min(i.get("trades", 0), o.get("trades", 0)) < min_trades:
        return -9.0
    dd = max(i["max_dd_usd"], o["max_dd_usd"])
    wr = min(i["win_rate"], o["win_rate"])
    net = min(i["net_usd"], o["net_usd"])
    s = wr + 0.15 * (i["best_trade_usd"] >= GOALS["best_trade_usd"]) + 0.05 * max(-1.0, min(1.0, net / 20_000))
    if dd >= GOALS["max_drawdown_usd"]:
        s -= 1.0 + (dd - GOALS["max_drawdown_usd"]) / 10_000
    if net <= 0:
        s -= 0.25
    return s


def search(n: int = 400, seed: int = 7, workers: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    cands = [DEFAULT_PARAMS] + [sample(rng) for _ in range(n - 1)]
    workers = workers or max(1, (os.cpu_count() or 2) - 0)
    with ProcessPoolExecutor(workers, initializer=_init) as ex:
        results = list(ex.map(evaluate, cands, chunksize=4))
    return sorted(results, key=lambda r: r["score"], reverse=True)


def refine(best: dict, n: int = 200, seed: int = 11, workers: int | None = None) -> list[dict]:
    """Local search: mutate one or two knobs of the incumbent at a time."""
    rng = random.Random(seed)
    cands = []
    for _ in range(n):
        p = copy.deepcopy(best["params"])
        for _ in range(rng.choice([1, 2])):
            book = rng.choice(list(SPACE))
            k = rng.choice(list(SPACE[book]))
            p[book][k] = rng.choice(SPACE[book][k])
        cands.append(p)
    workers = workers or max(1, os.cpu_count() or 2)
    with ProcessPoolExecutor(workers, initializer=_init) as ex:
        results = list(ex.map(evaluate, cands, chunksize=4))
    return sorted(results + [best], key=lambda r: r["score"], reverse=True)


def save(best: dict, path=CONFIG_PATH) -> None:
    path.write_text(json.dumps({"params": best["params"], "is": best["is"], "oos": best["oos"],
                                "score": best["score"]}, indent=2, default=float))


def table(results: list[dict], top: int = 15) -> pd.DataFrame:
    rows = []
    for r in results[:top]:
        rows.append({"score": r["score"], **{f"is_{k}": r["is"].get(k) for k in
                     ("trades", "win_rate", "net_usd", "max_dd_usd", "best_trade_usd")},
                     **{f"oos_{k}": r["oos"].get(k) for k in ("trades", "win_rate", "net_usd", "max_dd_usd")}})
    return pd.DataFrame(rows)
