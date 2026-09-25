"""``rapier`` command line: fetch, backtest, optimize, live, IBKR data, broker trading."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from . import data as D


def _market(base_tf: str, refresh: bool, source: str = "yahoo"):
    from .backtest import Market

    lower = {"1h": (), "5m": ("15m", "5m"), "1m": ("15m", "5m", "1m")}[base_tf]
    if source == "ibkr":
        from .feeds.ibkr import load_ibkr_1m

        m1 = load_ibkr_1m()
        if m1 is None:
            raise SystemExit("no IBKR history yet: run `rapier ibkr-backfill --start 2024-10-01` first")
        long_h1, rolls = D.roll_adjust(D.fetch("1h", refresh=refresh))
        fr = D.frames_from_1m(m1, long_h1)
        return Market.from_1h(fr["1h"], {tf: fr[tf] for tf in lower}, base_tf=base_tf), rolls
    d = D.load_nq(("1h",) + lower, refresh=refresh)
    return Market.from_1h(d["1h"], {tf: d[tf] for tf in lower}, base_tf=base_tf), d["rolls"]


def cmd_fetch(a):
    for iv in ("1h", "15m", "5m", "1m"):
        df = D.fetch(iv)
        print(f"{iv}: {len(df)} bars {df.index[0]} -> {df.index[-1]}")


def cmd_import(a):
    for path in a.paths:
        added, rejected = D.import_tape(path, a.interval)
        print(f"{path}: +{added} bars, {rejected} non-native day(s) rejected")


def cmd_backtest(a):
    from .report import write
    from .system import build, load_params

    params = load_params(a.config)
    if a.mode == "prop":
        params["risk"]["swing_overnight"] = False
    elif a.mode == "swing":
        params["risk"]["swing_overnight"] = True
    mkt, rolls = _market(a.base, refresh=not a.cached, source=a.source)
    books, risk = build(params, include_scalp=a.base != "1h", include_teacher=a.base == "1m")
    from .backtest import run

    res = run(mkt, books, risk, start=a.start, end=a.end)
    title = f"Prop Firm Rapier | {a.start} -> {a.end or 'today'} | base {a.base} | {a.mode} | {a.source}"
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


def _ibkr_feed(a):
    from .feeds.ibkr import IBKRFeed

    return IBKRFeed(host=a.host, port=a.port, client_id=a.client_id)


def cmd_ibkr_backfill(a):
    _ibkr_feed(a).connect().backfill(a.start, a.end)


def cmd_tradara_login(a):
    from .brokers import tradara as T

    verifier, challenge = T.pkce_pair()
    print("1) Open this URL in the browser where you are logged in to Tradara and approve access:\n")
    print(T.authorize_url(challenge), "\n")
    code = input("2) Paste the code Tradara shows (it is not echoed anywhere else): ").strip()
    path = Path(os.environ.get("RAPIER_TRADARA_TOKEN_FILE") or T.DEFAULT_TOKEN_FILE)
    T.exchange_code(code, verifier, path)
    print(f"saved tokens to {path} (chmod 600). Next: rapier broker-check --broker tradara")


def cmd_broker_check(a):
    from .trader import make_broker

    b = make_broker(a.broker)
    print(json.dumps(b.check(), indent=2, default=str))


def cmd_trade(a):
    from .executor import ExecConfig
    from .trader import run as run_trader

    if a.arm and os.environ.get("RAPIER_I_UNDERSTAND_REAL_ORDERS") != "yes":
        raise SystemExit("arming sends real orders: set RAPIER_I_UNDERSTAND_REAL_ORDERS=yes as well as --arm")
    cfg = ExecConfig(live_books=tuple(a.books.split(",")), max_qty=a.max_qty,
                     daily_loss_limit=a.daily_loss_limit)
    run_trader(broker_kind=a.broker, feed=a.feed, armed=a.arm, once=a.once, config=a.config, cfg=cfg)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser("rapier", description="Prop Firm Rapier - Bias + OTE bot for NQ")
    sub = p.add_subparsers(required=True)

    f = sub.add_parser("fetch", help="download / extend the local bar cache")
    f.set_defaults(fn=cmd_fetch)

    im = sub.add_parser("import", help="merge saved OHLCV tapes (CSV) into the cache")
    im.add_argument("interval", choices=["1m", "5m", "15m", "1h"])
    im.add_argument("paths", nargs="+")
    im.set_defaults(fn=cmd_import)

    b = sub.add_parser("backtest", help="run the backtest and write a report")
    b.add_argument("--start", default="2025-07-26")
    b.add_argument("--end", default=None)
    b.add_argument("--base", default="1h", choices=["1h", "5m", "1m"],
                   help="execution clock; 5m adds the scalp books, 1m also the teacher 1m OTE book")
    b.add_argument("--mode", default="config", choices=["config", "prop", "swing"],
                   help="prop = everything flat daily; swing = swing book may hold overnight")
    b.add_argument("--config", default=None)
    b.add_argument("--out", default="results/latest")
    b.add_argument("--cached", action="store_true", help="don't hit the network")
    b.add_argument("--source", default="yahoo", choices=["yahoo", "ibkr"],
                   help="ibkr = continuous 1m history from `rapier ibkr-backfill` (all timeframes resampled from it)")
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

    def ib_args(sp, port):
        sp.add_argument("--host", default=os.environ.get("RAPIER_IBKR_HOST", "127.0.0.1"))
        sp.add_argument("--port", type=int, default=port, help="IB Gateway live 4001 / paper 4002, TWS 7496 / 7497")
        sp.add_argument("--client-id", type=int, default=71)

    bf = sub.add_parser("ibkr-backfill", help="download continuous 1m NQ history from IBKR (resumable)")
    bf.add_argument("--start", required=True)
    bf.add_argument("--end", default=None)
    ib_args(bf, int(os.environ.get("RAPIER_IBKR_DATA_PORT", 4001)))
    bf.set_defaults(fn=cmd_ibkr_backfill)

    tl = sub.add_parser("tradara-login", help="OAuth (PKCE) login; stores tokens in a local 600 file")
    tl.set_defaults(fn=cmd_tradara_login)

    bc = sub.add_parser("broker-check", help="auth + account + instrument + position; sends no orders")
    bc.add_argument("--broker", required=True, choices=["tradara", "ibkr-paper"])
    bc.set_defaults(fn=cmd_broker_check)

    tr = sub.add_parser("trade", help="live loop: IBKR data -> engine -> broker (dry-run unless --arm)")
    tr.add_argument("--broker", default="none", choices=["none", "tradara", "ibkr-paper"])
    tr.add_argument("--feed", default="ibkr", choices=["ibkr", "yahoo"])
    tr.add_argument("--arm", action="store_true", help="send real orders (also needs RAPIER_I_UNDERSTAND_REAL_ORDERS=yes)")
    tr.add_argument("--books", default="teacher-1m,scalp-5m,ote-1h")
    tr.add_argument("--max-qty", type=int, default=10)
    tr.add_argument("--daily-loss-limit", type=float, default=600.0)
    tr.add_argument("--once", action="store_true")
    tr.add_argument("--config", default=None)
    tr.set_defaults(fn=cmd_trade)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
