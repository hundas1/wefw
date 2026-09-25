import json
import time

import pandas as pd
import pytest

from rapier import data as D
from rapier.brokers import tradara as T
from rapier.feeds import ibkr as I


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body
        self.content = b"x" if body is not None else b""
        self.text = json.dumps(body)

    def json(self):
        return self._b


class FakeSession:
    def __init__(self):
        self.headers, self.calls, self.orders = {}, [], []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data))
        return Resp(200, {"access_token": "new", "refresh_token": "r2", "expires_in": 3600})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers))
        path = url.replace(T.API, "")
        if path == "/v1/instruments":
            return Resp(200, {"items": [{"symbol": "/MNQZ26", "instrument_id": "iid-mnq"},
                                        {"symbol": "/NQZ26", "instrument_id": "iid-nq"}]})
        if path == "/v1/positions":
            return Resp(200, {"items": [{"instrument_id": "iid-mnq", "quantity": 3, "side": "LONG"}]})
        if path == "/v1/orders" and method == "GET":
            return Resp(200, {"items": [
                {"id": "1", "client_order_id": "rp-aaa-e", "status": "WORKING"},
                {"id": "2", "client_order_id": "rp-aaa-tp", "status": "WORKING"},
                {"id": "3", "client_order_id": "rp-bbb-sl", "status": "WORKING"},
                {"id": "4", "client_order_id": "manual-1", "status": "WORKING"},
                {"id": "5", "client_order_id": "rp-ccc-e", "status": "FILLED"}]})
        if path == "/v1/orders/groups":
            self.orders.append(json)
            return Resp(200, {"item": {"id": "grp-1"}})
        if path == "/v1/balances":
            return Resp(200, {"items": [{"account_id": "acct-1", "balance": "50012.50"}]})
        return Resp(200, {})


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPIER_TRADARA_ALLOWED_ACCOUNTS", "acct-1")
    tok = tmp_path / "tok.json"
    tok.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                               "obtained_at": time.time()}))
    return T.TradaraBroker("acct-1", root="MNQ", token_file=tok, session=FakeSession(),
                           contract_symbol="/MNQZ26")


def test_tradara_refuses_unlisted_account(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIER_TRADARA_ALLOWED_ACCOUNTS", "acct-1")
    with pytest.raises(T.TradaraError):
        T.TradaraBroker("someone-else", token_file=tmp_path / "t.json", session=FakeSession())


def test_tradara_bracket_payload(broker):
    broker.place_bracket("rp-abc", -1, 3, 20000.1, 20030.0, 19970.0)
    body = broker.s.orders[-1]
    assert body["kind"] == "OTOCO" and body["account_id"] == "acct-1"
    e, tp, sl = body["orders"]
    assert (e["role"], e["side"], e["execution_type"], e["limit_price"], e["quantity"]) == ("ENTRY", "SELL", "LIMIT", 20000.0, 3)
    assert (tp["side"], tp["limit_price"], tp["position_effect"]) == ("BUY", 19970.0, "CLOSE")
    assert (sl["execution_type"], sl["stop_price"]) == ("STOP", 20030.0)
    assert all(o["instrument_id"] == "iid-mnq" for o in body["orders"])
    hdr = [c for c in broker.s.calls if c[0] == "POST" and c[1].endswith("/v1/orders/groups")][-1][3]
    assert hdr["Idempotency-Key"] == "rapier-rp-abc"
    broker.place_bracket("rp-mkt", 1, 1, None, 19990, 20010)
    assert broker.s.orders[-1]["orders"][0]["execution_type"] == "MARKET"
    assert "limit_price" not in broker.s.orders[-1]["orders"][0]


def test_tradara_state_parsing(broker):
    assert broker.position() == 3
    g = broker.groups()
    assert set(g) == {"rp-aaa", "rp-bbb"}  # manual and filled orders ignored
    assert g["rp-aaa"]["state"] == "working" and g["rp-bbb"]["state"] == "active"


def test_tradara_refreshes_expired_token(broker):
    tok = json.loads(broker.token_file.read_text())
    tok["obtained_at"] = 0
    broker.token_file.write_text(json.dumps(tok))
    broker._access = None
    broker.position()
    assert any(c[0] == "POST" and c[1] == T.TOKEN_URL for c in broker.s.calls)
    saved = json.loads(broker.token_file.read_text())
    assert saved["access_token"] == "new" and saved["refresh_token"] == "r2"
    assert oct(broker.token_file.stat().st_mode & 0o777) == "0o600"


def test_pkce_and_authorize_url():
    v, c = T.pkce_pair()
    assert 43 <= len(v) <= 128 and "=" not in c
    url = T.authorize_url(c)
    assert url.startswith(T.AUTHORIZE_URL) and "code_challenge_method=S256" in url


# ------------------------------------------------------------------ IBKR data
def test_roll_schedule_matches_rapier_convention():
    import datetime as dt

    assert I.switch_time(dt.date(2026, 9, 18)) == pd.Timestamp("2026-09-13 18:00", tz=D.TZ)
    assert I.front_expiry(pd.Timestamp("2026-09-13 17:59", tz=D.TZ)) == dt.date(2026, 9, 18)
    assert I.front_expiry(pd.Timestamp("2026-09-13 18:00", tz=D.TZ)) == dt.date(2026, 12, 18)
    sch = I.schedule(pd.Timestamp("2026-08-01", tz=D.TZ), pd.Timestamp("2026-10-01", tz=D.TZ))
    assert [e for e, _, _ in sch] == [dt.date(2026, 9, 18), dt.date(2026, 12, 18)]


def test_stitch_uses_measured_spread():
    import datetime as dt

    idx0 = pd.date_range("2026-09-11 15:00", periods=120, freq="1min", tz=D.TZ)
    idx1 = pd.date_range("2026-09-13 18:00", periods=60, freq="1min", tz=D.TZ)
    mk = lambda idx, px: pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1.0}, index=idx)
    old = mk(idx0, 20000.0)
    new = mk(idx1, 20300.0)
    overlap_next = mk(idx0, 20287.5)  # next contract during the last session of the old one
    df, spreads = I.stitch([(dt.date(2026, 9, 18), old), (dt.date(2026, 12, 18), new)],
                           {dt.date(2026, 9, 18): overlap_next})
    assert spreads == {"2026-09-18": 287.5}
    assert df.close.loc[idx0].eq(20287.5).all() and df.close.loc[idx1].eq(20300.0).all()


def test_frames_from_1m_splices_long_history():
    m1 = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0},
                      index=pd.date_range("2026-09-14 09:00", periods=600, freq="1min", tz=D.TZ))
    h1 = pd.DataFrame({"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 1.0},
                      index=pd.date_range("2026-09-01", "2026-09-15", freq="1h", tz=D.TZ))
    fr = D.frames_from_1m(m1, h1)
    assert fr["1h"].index[0] == h1.index[0]
    assert fr["1h"].close.loc[: "2026-09-14 08:00"].eq(100.0).all()  # shifted by the overlap difference
    assert set(fr) == {"1m", "5m", "15m", "1h"}


def test_ibkr_fill_price_and_target_amend():
    from types import SimpleNamespace as NS

    from rapier.brokers.ibkr import IBKRBroker

    def trade(pid, typ, status, avg=0.0, px=None):
        o = NS(orderRef="rp-a", parentId=pid, orderType=typ, lmtPrice=px, transmit=False)
        return NS(order=o, orderStatus=NS(status=status, avgFillPrice=avg), isDone=lambda: status == "Filled")

    parent, tp = trade(0, "MKT", "Filled", 20014.5), trade(1, "LMT", "Submitted", px=20041.5)
    placed = []
    ib = NS(isConnected=lambda: True, trades=lambda: [parent, tp, trade(1, "STP", "Submitted")],
            placeOrder=lambda c, o: placed.append(o), sleep=lambda s: None)
    b = IBKRBroker(ib=ib)
    b._c = NS(conId=1)
    b.contract = lambda: b._c
    assert b.fill_price("rp-a") == 20014.5
    assert b.fill_price("rp-zz") is None
    assert b.amend_target("rp-a", 20049.0) and placed == [tp.order]
    assert tp.order.lmtPrice == 20049.0 and tp.order.transmit
