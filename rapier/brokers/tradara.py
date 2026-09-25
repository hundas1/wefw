"""Tradara Trading REST adapter (Lucid / prop accounts routed through Tradara).

Ported from the original Bee Sid bot's client, which placed OTOCO brackets on a
Tradara practice account. Hard rules kept from it:

* orders only go to an account listed in ``RAPIER_TRADARA_ALLOWED_ACCOUNTS``;
* the account-level flatten endpoint is never used - Rapier only cancels its
  own ``rp-`` orders and closes the NQ/MNQ position it opened;
* tokens are read from a local file (``RAPIER_TRADARA_TOKEN_FILE``, chmod 600)
  and never printed.

Known Tradara behaviour (from that bot's logs): the MCP endpoint rejects valid
tokens, so only the Trading REST API is used; Cloudflare wants a browser-like
User-Agent; refresh tokens have been seen to die with HTTP 401, which is why
``check()`` must pass before arming and every cycle re-verifies access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import pandas as pd

from .. import data as D
from .base import NetPosition, RoundTick

log = logging.getLogger("rapier.tradara")

API = "https://api.tradara.com"
TOKEN_URL = f"{API}/v1/auth/mcp/token"
AUTHORIZE_URL = "https://auth.tradara.com/v1/auth/mcp/authorize"
CLIENT_ID = "terminal-cursor"
RESOURCE = "https://mcp.tradara.com"
REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
SCOPES = "accounts:read orders:read orders:write orders:cancel positions:read fills:read"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")
OPEN = {"SUBMITTED", "ROUTED", "WORKING", "OPEN", "NEW", "PARTIALLY_FILLED", "PENDING", "ACCEPTED"}
DEFAULT_TOKEN_FILE = D.DATA_DIR / "tradara-token.json"


class TradaraError(RuntimeError):
    pass


def AllowedAccounts() -> set[str]:
    return {a.strip() for a in os.environ.get("RAPIER_TRADARA_ALLOWED_ACCOUNTS", "").split(",") if a.strip()}


class TradaraBroker:
    name = "tradara"

    def __init__(self, account_id: str, root: str = "MNQ", token_file: str | Path | None = None,
                 session=None, contract_symbol: str | None = None):
        if account_id not in AllowedAccounts():
            raise TradaraError(f"account {account_id!r} is not in RAPIER_TRADARA_ALLOWED_ACCOUNTS")
        if root not in ("MNQ", "NQ"):
            raise TradaraError("root must be MNQ or NQ")
        import requests

        self.account_id = account_id
        self.root = root
        self.units_per_contract = 1 if root == "MNQ" else 10
        self.token_file = Path(token_file or os.environ.get("RAPIER_TRADARA_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json",
                               "X-Order-Origin": "prop-firm-rapier"})
        self.contract_symbol = contract_symbol
        self._iid: str | None = None
        self._access: str | None = None
        self._exp = 0.0
        self._groups: dict[str, str] = {}  # prefix -> group id
        self._start_balance: tuple[str, float] | None = None

    # ------------------------------------------------------------------ auth
    def _Tokens(self) -> dict:
        if not self.token_file.exists():
            raise TradaraError(f"no token file at {self.token_file}; run `rapier tradara-login`")
        return json.loads(self.token_file.read_text())

    def _Refresh(self, force: bool = False) -> None:
        tok = self._Tokens()
        exp = float(tok.get("obtained_at", 0)) + float(tok.get("expires_in", 3600)) - 120
        if not force and time.time() < exp:
            self._access, self._exp = tok["access_token"], exp
            return
        r = self.s.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                         "client_id": CLIENT_ID, "resource": RESOURCE},
                        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        if r.status_code >= 400:
            raise TradaraError(f"token refresh failed (HTTP {r.status_code}); run `rapier tradara-login` again")
        body = r.json()
        SaveTokens(self.token_file, body, tok)
        self._access = body["access_token"]
        self._exp = time.time() + float(body.get("expires_in", 3600)) - 120

    def _Req(self, method: str, path: str, *, params=None, body=None, idem: str | None = None):
        if body and body.get("account_id") not in (None, self.account_id):
            raise TradaraError("refusing an order for a different account")
        if not self._access or time.time() >= self._exp:
            self._Refresh()
        for attempt in (0, 1):
            h = {"Authorization": f"Bearer {self._access}"}
            if idem:
                h["Idempotency-Key"] = idem
            if body is not None:
                h["Content-Type"] = "application/json"
            r = self.s.request(method, f"{API}{path}", params=params, json=body, headers=h, timeout=30)
            if r.status_code == 401 and attempt == 0:
                self._Refresh(force=True)
                continue
            break
        if r.status_code >= 400:
            raise TradaraError(f"{method} {path} HTTP {r.status_code}: {(r.text or '')[:300]}")
        return r.json() if r.content else None

    # ------------------------------------------------------------ instrument
    def InstrumentId(self) -> str:
        if self._iid:
            return self._iid
        from ..feeds.ibkr import FrontExpiry

        exp = FrontExpiry(pd.Timestamp.now(tz=D.TZ))
        code = "FGHJKMNQUVXZ"[exp.month - 1]
        want = self.contract_symbol or f"/{self.root}{code}{exp.year % 100:02d}"
        items = (self._Req("GET", "/v1/instruments", params={"q": self.root, "limit": 100}) or {}).get("items") or []
        for it in items:
            sym = str(it.get("symbol") or "").upper()
            if sym in (want.upper(), want.upper().lstrip("/")):
                self._iid, self.contract_symbol = str(it["instrument_id"]), want
                return self._iid
        raise TradaraError(f"Tradara instrument {want} not found")

    # ------------------------------------------------------------- interface
    def Check(self) -> dict:
        self._Refresh()
        bal = self._Balance()
        return {"broker": "tradara", "account": self.account_id, "instrument": self.contract_symbol,
                "instrument_id": self.InstrumentId(), "balance": bal, "position": self.Position(),
                "rapier_open_orders": len(self._OpenOrders())}

    def _Balance(self) -> float | None:
        items = (self._Req("GET", "/v1/balances", params={"account_id": self.account_id}) or {}).get("items") or []
        for it in items:
            if str(it.get("account_id")) == self.account_id:
                try:
                    return float(it.get("balance"))
                except (TypeError, ValueError):
                    return None
        return None

    def _OpenOrders(self) -> list[dict]:
        items = (self._Req("GET", "/v1/orders", params={"account_id": self.account_id}) or {}).get("items") or []
        return [o for o in items if str(o.get("status") or "").upper() in OPEN
                and str(o.get("client_order_id") or "").startswith("rp-")]

    def Position(self) -> int:
        items = (self._Req("GET", "/v1/positions", params={"account_id": self.account_id}) or {}).get("items") or []
        return NetPosition(items, self.InstrumentId(), self.root)

    def Groups(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for o in self._OpenOrders():
            coid = str(o["client_order_id"])
            prefix, _, leg = coid.rpartition("-")
            g = out.setdefault(prefix, {"state": "active", "orders": []})
            g["orders"].append(o.get("id"))
            if leg == "e":
                g["state"] = "working"
        return out

    def PlaceBracket(self, prefix, side, qty, entry, stop, target):
        iid = self.InstrumentId()
        es, xs = ("BUY", "SELL") if side > 0 else ("SELL", "BUY")
        leg = lambda role, s, typ, suffix, **px: {  # noqa: E731
            "role": role, "account_id": self.account_id, "instrument_id": iid, "side": s,
            "execution_type": typ, "quantity": int(qty), "time_in_force": "GTC" if typ != "MARKET" else "DAY",
            "position_effect": "OPEN" if role == "ENTRY" else "CLOSE", "client_order_id": f"{prefix}-{suffix}",
            **{k: RoundTick(v) for k, v in px.items()}}
        entry_leg = leg("ENTRY", es, "MARKET", "e") if entry is None else leg("ENTRY", es, "LIMIT", "e", limit_price=entry)
        body = {"account_id": self.account_id, "kind": "OTOCO", "orders": [
            entry_leg,
            leg("TAKE_PROFIT", xs, "LIMIT", "tp", limit_price=target),
            leg("STOP_LOSS", xs, "STOP", "sl", stop_price=stop)]}
        data = self._Req("POST", "/v1/orders/groups", body=body, idem=f"rapier-{prefix}") or {}
        item = data.get("item") if isinstance(data.get("item"), dict) else data
        gid = item.get("id") or item.get("group_id")
        if gid:
            self._groups[prefix] = str(gid)

    def Cancel(self, prefix):
        gid = self._groups.pop(prefix, None)
        if gid:
            self._Req("DELETE", f"/v1/orders/groups/{gid}", params={"account_id": self.account_id})
            return
        for o in self._OpenOrders():
            if str(o["client_order_id"]).startswith(prefix + "-"):
                self._Req("POST", f"/v1/orders/{o['id']}/cancel", body={}, idem=f"rapier-cx-{o['id']}")

    def Flatten(self):
        for prefix in list(self.Groups()):
            self.Cancel(prefix)
        pos = self.Position()
        if pos:
            self._Req("POST", "/v1/orders", idem=f"rapier-flat-{int(time.time())}", body={
                "account_id": self.account_id, "instrument_id": self.InstrumentId(),
                "side": "SELL" if pos > 0 else "BUY", "execution_type": "MARKET", "quantity": abs(pos),
                "time_in_force": "DAY", "position_effect": "CLOSE",
                "client_order_id": f"rp-flat-{int(time.time())}"})

    def DayPnl(self) -> float | None:
        """Balance change since the first check of the current trading day."""
        bal = self._Balance()
        if bal is None:
            return None
        day = str(D.TradingDay(pd.DatetimeIndex([pd.Timestamp.now(tz=D.TZ)]))[0].date())
        if not self._start_balance or self._start_balance[0] != day:
            self._start_balance = (day, bal)
        return bal - self._start_balance[1]


# ------------------------------------------------------------------ login
def SaveTokens(path: Path, body: dict, prev: dict | None = None) -> None:
    prev = prev or {}
    out = {"access_token": body["access_token"],
           "refresh_token": body.get("refresh_token") or prev.get("refresh_token"),
           "expires_in": body.get("expires_in", 3600), "token_type": body.get("token_type", "Bearer"),
           "scope": body.get("scope", prev.get("scope")), "obtained_at": time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    os.chmod(path, 0o600)


def PkcePair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def AuthorizeUrl(challenge: str) -> str:
    q = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": SCOPES,
         "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE,
         "state": secrets.token_urlsafe(12)}
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(q)}"


def ExchangeCode(code: str, verifier: str, token_file: Path, session=None) -> None:
    import requests

    s = session or requests.Session()
    r = s.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code.strip(), "client_id": CLIENT_ID,
                                "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": RESOURCE},
               headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}, timeout=30)
    if r.status_code >= 400:
        raise TradaraError(f"code exchange failed (HTTP {r.status_code}): {(r.text or '')[:200]}")
    SaveTokens(token_file, r.json())
