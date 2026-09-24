"""``rapier`` command line: fetch, backtest, optimize, live."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import data as D


def _market(base_tf: str, refresh: bool):
    from .backtest import Market

    lower = () if base_tf == "1h" else ("15m", "5m")
    d = D.load_nq(("1h",) + lower, refresh=refresh)
    return Market.from_1h(d["1h"], {tf: d[tf] for tf in lower}, base_tf=base_tf), d["rolls"]


def cmd_fetch(a):
    for iv in ("1h", "15m", "5m"):
        df = D.fetch(iv)
        print(f"{iv}: {len(df)} bars {df.index[0]} -> {df.index[-1]}")


def cmd_backtest(a):
    from .report import write
    from .system import build, load_params

    params = load_params(a.config)
    if a.mode == "prop":
        params["risk"]["swing_overnight"] = False
    elif a.mode == "swing":
        params["risk"]["swing_overnight"] = True
    mkt, rolls = _market(a.base, refresh=not a.cached)
    books, risk = build(params, include_scalp=a.base != "1h")
    from .backtest import run

    res = run(mkt, books, risk, start=a.start, end=a.end)
    title = f"Prop Firm Rapier | {a.start} -> {a.end or 'today'} | base {a.base} | {a.mode}"
    out = write(res, a.out, title)
    print(json.dumps(out, indent=2, default=float))
    print("rolls:", [(str(r.expiry), str(r.switch), r.spread, r.method) for r in rolls])


def cmd_optimize(a):
    from . import optimize as O

    res = O.search(a.n, seed=a.seed)
    best = res[0]
    for i in range(a.refine):
        best = O.refine(best, a.n // 4, seed=a.seed + 1 + i)[0]
    print(O.table([best] + res, 10).round(3).to_string())
    if a.save:
        O.save(best)
        print("saved", O.CONFIG_PATH)


def cmd_live(a):
    from .live import loop

    loop(interval=a.interval, once=a.once, config=a.config)


def main(argv=None):
    p = argparse.ArgumentParser("rapier", description="Prop Firm Rapier - Bias + OTE bot for NQ")
    sub = p.add_subparsers(required=True)

    f = sub.add_parser("fetch", help="download / extend the local bar cache")
    f.set_defaults(fn=cmd_fetch)

    b = sub.add_parser("backtest", help="run the backtest and write a report")
    b.add_argument("--start", default="2025-07-26")
    b.add_argument("--end", default=None)
    b.add_argument("--base", default="1h", choices=["1h", "5m"],
                   help="execution clock; 5m enables the 15m/5m scalp books (~60 days of Yahoo data)")
    b.add_argument("--mode", default="config", choices=["config", "prop", "swing"],
                   help="prop = everything flat daily; swing = swing book may hold overnight")
    b.add_argument("--config", default=None)
    b.add_argument("--out", default="results/latest")
    b.add_argument("--cached", action="store_true", help="don't hit the network")
    b.set_defaults(fn=cmd_backtest)

    o = sub.add_parser("optimize", help="refinement search with out-of-sample guard")
    o.add_argument("--n", type=int, default=2000)
    o.add_argument("--refine", type=int, default=4)
    o.add_argument("--seed", type=int, default=21)
    o.add_argument("--save", action="store_true")
    o.set_defaults(fn=cmd_optimize)

    lv = sub.add_parser("live", help="paper-trade / signal loop")
    lv.add_argument("--interval", type=int, default=300)
    lv.add_argument("--once", action="store_true")
    lv.add_argument("--config", default=None)
    lv.set_defaults(fn=cmd_live)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
