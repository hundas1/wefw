"""Live trading loop: IBKR 1m bars -> Rapier engine -> executor -> broker.

Runs once per completed 1m bar (a few seconds after the minute). The data
connection to IBKR is read-only; orders go only through the chosen broker.

``armed=False`` (default) is a dry run: real state is read, orders are only
logged. Arming requires a realtime feed (IBKR) - Yahoo is delayed and can
never drive real orders.
"""

from __future__ import annotations

import logging
import os
import time

import pandas as pd

from . import data as D
from .backtest import Market
from .brokers.base import Broker, DryRunBroker
from .executor import ExecConfig, Executor
from .live import BuildMarket, EngineView, Notify
from .system import LoadParams

log = logging.getLogger("rapier.trader")
EXEC_STATE = D.DATA_DIR / "exec_state.json"
LOOKBACK_1M_DAYS = 40


class IBKRSource:
    """Continuous back-adjusted 1m history + live polling of the front contract."""

    def __init__(self, feed):
        from .feeds.ibkr import FrontExpiry

        self.feed = feed.Connect()
        self._front = FrontExpiry
        self.expiry = None
        self.m1 = None
        self.long_h1 = None

    def _Init(self):
        now = pd.Timestamp.now(tz=D.TZ)
        self.expiry = self._front(now)
        self.m1 = self.feed.Backfill(now - pd.Timedelta(days=LOOKBACK_1M_DAYS + 5), progress=log.info)
        self.long_h1, _ = D.RollAdjust(D.Fetch("1h"))

    def Market(self, params) -> Market:
        now = pd.Timestamp.now(tz=D.TZ)
        if self.m1 is None or self._front(now) != self.expiry:
            self._Init()  # first run, or the contract just rolled: re-stitch
        new = self.feed.Poll(self.expiry)
        m1 = pd.concat([self.m1, new])
        self.m1 = m1[~m1.index.duplicated(keep="last")].sort_index()
        self.m1 = self.m1[self.m1.index >= now - pd.Timedelta(days=LOOKBACK_1M_DAYS)]
        fr = D.FramesFrom1m(self.m1, self.long_h1)
        return Market.From1h(fr["1h"], {k: fr[k] for k in ("1m", "5m", "15m")}, base_tf="1m")


class YahooSource:
    """Delayed data: dry runs only."""

    def Market(self, params) -> Market:
        return BuildMarket(params, refresh=True)


def MakeBroker(kind: str) -> Broker | None:
    root = os.environ.get("RAPIER_ROOT", "MNQ")
    if kind == "tradara":
        from .brokers.tradara import TradaraBroker

        return TradaraBroker(os.environ["RAPIER_TRADARA_ACCOUNT"], root=root)
    if kind == "ibkr-paper":
        from .brokers.ibkr import IBKRBroker

        return IBKRBroker(port=int(os.environ.get("RAPIER_IBKR_ORDER_PORT", 4002)), root=root)
    if kind == "none":
        return None
    raise ValueError(kind)


def Run(broker_kind: str = "none", feed: str = "ibkr", armed: bool = False, once: bool = False,
        config: str | None = None, cfg: ExecConfig | None = None) -> None:
    if armed and feed != "ibkr":
        raise SystemExit("refusing to arm on a delayed feed: use --feed ibkr")
    if armed and broker_kind == "none":
        raise SystemExit("choose a broker to arm")
    params = LoadParams(config)
    params["teacher"]["enabled"] = True
    params["scalp"]["enabled"] = True
    inner = MakeBroker(broker_kind)
    if inner is not None:
        log.info("broker check: %s", inner.Check())  # fails loudly on auth/account problems
    broker = inner if armed else DryRunBroker(inner)
    ex = Executor(broker, cfg or ExecConfig(), EXEC_STATE if armed else None)
    if feed == "ibkr":
        from .feeds.ibkr import IBKRFeed

        src = IBKRSource(IBKRFeed(host=os.environ.get("RAPIER_IBKR_HOST", "127.0.0.1"),
                                  port=int(os.environ.get("RAPIER_IBKR_DATA_PORT", 4001))))
    else:
        src = YahooSource()
    webhook = os.environ.get("RAPIER_DISCORD_WEBHOOK")
    mode = "ARMED" if armed else "dry-run"
    Notify({"type": "START", "mode": mode, "broker": broker_kind, "feed": feed,
            "books": ",".join(ex.cfg.live_books)}, webhook)
    while True:
        try:
            view, _ = EngineView(src.Market(params), params)
            for msg in ex.Step(view):
                Notify({"type": "EXEC", "mode": mode, "msg": msg}, webhook)
        except Exception as e:  # keep running; brackets at the broker protect open positions
            log.exception("cycle failed")
            Notify({"type": "ERROR", "mode": mode, "error": repr(e)[:300]}, webhook)
        if once:
            return
        # wake 3 seconds after the next minute, when the bar that just closed is available
        time.sleep(60 - (time.time() % 60) + 3)
