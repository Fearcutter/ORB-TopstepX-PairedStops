"""TopstepX API client — REST + SignalR.

Thin wrapper around the ProjectX Gateway (which TopstepX uses). Uses httpx
for REST (sync) and signalrcore for the SignalR WebSocket.

Endpoints are the public ProjectX Gateway shape:
  https://gateway.docs.projectx.com/docs/api-reference/

If an endpoint path or payload shape differs on the user's environment, the
HTTP error will surface the problem — adjust the relevant method here and
let the caller retry. All methods raise RuntimeError with a descriptive
message on failure; the UI reports these verbatim.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

REST_BASE = "https://api.topstepx.com"
SIGNALR_HUB = "https://rtc.topstepx.com/hubs/user"


# -----------------------------------------------------------------------------
# Data classes returned to the rest of the app
# -----------------------------------------------------------------------------
@dataclass
class Account:
    id: str
    name: str
    raw: dict


@dataclass
class Contract:
    id: str
    symbol: str
    tick_size: float
    raw: dict


@dataclass
class Quote:
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]


# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------
class TopstepXClient:
    """Sync REST + background SignalR. Safe to use from the Qt main thread;
    SignalR callbacks arrive on the SignalR worker thread and must not touch
    Qt widgets directly — marshal via pyqtSignal."""

    def __init__(self, username: str, api_key: str, timeout: float = 10.0):
        if not username or not api_key:
            raise ValueError("TopstepX requires both username and api_key")
        self._username = username
        self._api_key = api_key
        self._token: Optional[str] = None
        self._token_issued_at: float = 0.0
        self._http = httpx.Client(base_url=REST_BASE, timeout=timeout)
        self._hub = None
        self._hub_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Authenticate and cache a bearer token."""
        resp = self._http.post(
            "/api/Auth/loginKey",
            json={"userName": self._username, "apiKey": self._api_key},
        )
        self._raise_for_status(resp, "login")
        body = resp.json()
        token = body.get("token") or body.get("jwt") or body.get("accessToken")
        if not token:
            raise RuntimeError(f"login: no token in response: {body!r}")
        self._token = token
        self._token_issued_at = time.time()
        logger.info("TopstepX login succeeded.")

    def _ensure_token(self) -> None:
        # Re-login well before the 24h expiry.
        if not self._token or (time.time() - self._token_issued_at) > 23 * 3600:
            self.connect()

    def _auth_headers(self) -> Dict[str, str]:
        self._ensure_token()
        return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------
    # REST wrappers
    # ------------------------------------------------------------------
    def list_accounts(self) -> List[Account]:
        resp = self._http.post(
            "/api/Account/search",
            headers=self._auth_headers(),
            json={"onlyActiveAccounts": True},
        )
        self._raise_for_status(resp, "list_accounts")
        body = resp.json()
        accounts_raw = body.get("accounts") or body.get("data") or []
        return [
            Account(id=str(a.get("id")), name=str(a.get("name", a.get("id"))), raw=a)
            for a in accounts_raw
        ]

    def lookup_contract(self, symbol: str) -> Contract:
        """Resolve a human symbol (e.g. 'NQ' or 'NQM6' or a full ContractId) to
        an active Contract. ProjectX returns multiple hits for a free-text
        search; we prefer the exact front-month active contract."""
        # `live: False` returns the canonical contracts list. `live: True` is
        # gated behind a live market-data entitlement most accounts don't have.
        resp = self._http.post(
            "/api/Contract/search",
            headers=self._auth_headers(),
            json={"searchText": symbol, "live": False},
        )
        self._raise_for_status(resp, "lookup_contract")
        body = resp.json()
        hits = body.get("contracts") or []
        if not hits:
            raise RuntimeError(f"No contract found for symbol '{symbol}'")

        needle = symbol.upper().strip()
        active = [c for c in hits if c.get("activeContract")]
        pool = active or hits

        # Priority order: exact name match > name-prefix match > symbolId ends > first.
        def _pick() -> dict:
            for c in pool:
                if str(c.get("name", "")).upper() == needle:
                    return c
            for c in pool:
                if str(c.get("name", "")).upper().startswith(needle):
                    return c
            for c in pool:
                sid = str(c.get("symbolId", "")).upper()
                if sid.endswith("." + needle) or sid.endswith(needle):
                    return c
            return pool[0]

        match = _pick()
        tick = float(match.get("tickSize") or 0.25)
        return Contract(
            id=str(match.get("id")),
            symbol=str(match.get("name", symbol)),
            tick_size=tick,
            raw=match,
        )

    def get_quote(self, contract_id: str) -> Quote:
        """Approximate a last-traded price via the most recent 1-minute bar.

        TopstepX has no REST quote endpoint; bid/ask/last live on the SignalR
        market-data stream only. Placing a pair only needs a reference price
        though, so we read the last minute bar's close from /History/retrieveBars.

        Returns bid/ask as None — callers should use `last` as the reference.
        """
        from datetime import datetime, timedelta, timezone
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=5)
            resp = self._http.post(
                "/api/History/retrieveBars",
                headers=self._auth_headers(),
                json={
                    "contractId": contract_id,
                    "live": False,
                    "startTime": start.isoformat(),
                    "endTime": now.isoformat(),
                    "unit": 2,           # Minute
                    "unitNumber": 1,
                    "limit": 5,
                    "includePartialBar": True,
                },
            )
            self._raise_for_status(resp, "get_quote")
            body = resp.json()
            bars = body.get("bars") or []
            if not bars:
                return Quote(last=None, bid=None, ask=None)
            # Response is sorted newest-first. Use the most recent bar's close.
            last = _maybe_float(bars[0].get("c"))
            return Quote(last=last, bid=None, ask=None)
        except Exception as ex:
            logger.warning("get_quote failed: %s", ex)
            return Quote(last=None, bid=None, ask=None)

    def place_stop_with_bracket(
        self,
        account_id: str,
        contract_id: str,
        side: str,   # "BUY" or "SELL"
        size: int,
        stop_price: float,
        tp_ticks: int,
        sl_ticks: int,
        custom_tag: str = "",
    ) -> dict:
        """Place a stop entry (type=4) with take-profit + stop-loss brackets.
        Brackets are in ticks from the fill price; ProjectX attaches them as
        exits that activate when the entry fills. OCO between the two pair
        legs is NOT handled here — PairManager cancels the partner on fill."""
        is_buy = side.upper() == "BUY"
        # Bracket ticks are SIGNED relative to the entry price:
        #   Long  (buy):  TP above entry → +ticks; SL below entry → −ticks.
        #   Short (sell): TP below entry → −ticks; SL above entry → +ticks.
        # Server explicitly rejects the wrong sign with:
        #   "Invalid stop loss ticks (N). Ticks should be less than zero when longing."
        tp_signed = abs(tp_ticks) if is_buy else -abs(tp_ticks)
        sl_signed = -abs(sl_ticks) if is_buy else abs(sl_ticks)

        payload = {
            "accountId": int(account_id),
            "contractId": contract_id,
            "type": 4,                                   # Stop per ProjectX enum
            "side": 0 if is_buy else 1,                  # 0=Bid/Buy, 1=Ask/Sell
            "size": size,
            "stopPrice": stop_price,
            # bracket `type` = ProjectX order-type enum:
            #   1 = Limit  (correct for take-profit)
            #   4 = Stop   (correct for stop-loss — server rejects Limit here)
            "takeProfitBracket": {"ticks": tp_signed, "type": 1},
            "stopLossBracket":   {"ticks": sl_signed, "type": 4},
        }
        if custom_tag:
            payload["customTag"] = custom_tag

        body = self._post_order(payload)

        # TopstepX refuses API brackets when the account has Position Brackets
        # (platform-level defaults) on. In that case, resubmit the entry WITHOUT
        # brackets and let Position Brackets attach exits automatically at fill.
        # Drop customTag too: the rejected attempt already "reserved" the tag,
        # and TopstepX enforces tag uniqueness even across failed placements.
        if (not body.get("success")
                and _is_position_brackets_conflict(body)):
            logger.info("Brackets rejected (Position Brackets on) — retrying without brackets.")
            payload.pop("takeProfitBracket", None)
            payload.pop("stopLossBracket", None)
            payload.pop("customTag", None)
            body = self._post_order(payload)

        self._raise_if_api_failure(body, "place_stop_with_bracket")
        return body

    def _post_order(self, payload: dict) -> dict:
        resp = self._http.post(
            "/api/Order/place", headers=self._auth_headers(), json=payload
        )
        self._raise_for_status(resp, "place_stop_with_bracket")
        return resp.json()

    def modify_order(
        self,
        account_id: str,
        order_id: str,
        stop_price: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> dict:
        payload = {"accountId": int(account_id), "orderId": int(order_id)}
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        resp = self._http.post(
            "/api/Order/modify", headers=self._auth_headers(), json=payload
        )
        self._raise_for_status(resp, "modify_order")
        body = resp.json()
        self._raise_if_api_failure(body, "modify_order")
        return body

    def cancel_order(self, account_id: str, order_id: str) -> dict:
        resp = self._http.post(
            "/api/Order/cancel",
            headers=self._auth_headers(),
            json={"accountId": int(account_id), "orderId": int(order_id)},
        )
        self._raise_for_status(resp, "cancel_order")
        body = resp.json()
        self._raise_if_api_failure(body, "cancel_order")
        return body

    # ------------------------------------------------------------------
    # SignalR — order event stream
    # ------------------------------------------------------------------
    def subscribe_order_events(
        self,
        account_id: str,
        on_order: Callable[[dict], None],
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        """Start a SignalR connection and subscribe to order events for the
        given account. `on_order` fires on the SignalR worker thread for every
        GatewayUserOrder event — callers must marshal to the UI thread via
        a pyqtSignal before touching widgets."""
        from signalrcore.hub_connection_builder import HubConnectionBuilder  # lazy import

        self._ensure_token()
        with self._hub_lock:
            if self._hub is not None:
                logger.info("SignalR already connected; reusing.")
                return

            # TopstepX/ProjectX SignalR requires:
            #   - wss:// URL (not https://)
            #   - token appended as ?access_token=<jwt> query parameter
            #   - skip_negotiation = True (the /negotiate endpoint is not used)
            # signalrcore's access_token_factory option hits the wrong path.
            wss_url = SIGNALR_HUB.replace("https://", "wss://", 1)
            full_url = f"{wss_url}?access_token={self._token}"

            hub = (
                HubConnectionBuilder()
                .with_url(full_url, options={
                    "verify_ssl": True,
                    "skip_negotiation": True,
                })
                .with_automatic_reconnect(
                    {"type": "interval", "keep_alive_interval": 10,
                     "intervals": [0, 2, 5, 10, 15, 30, 60]}
                )
                .build()
            )

            acct_int = int(account_id)

            def _on_open():
                logger.info("SignalR connected; subscribing to orders for account %s.", acct_int)
                # Correct method name per live probe of api.topstepx.com:
                # `SubscribeOrders` (plural, no "To" prefix) with [accountId].
                hub.send("SubscribeOrders", [acct_int])
                if on_connect:
                    on_connect()

            def _on_close():
                logger.warning("SignalR disconnected.")
                if on_disconnect:
                    on_disconnect()

            hub.on_open(_on_open)
            hub.on_close(_on_close)
            # ProjectX emits GatewayUserOrder for order events. The handler
            # receives args=[envelope] where envelope={"action": int, "data": {...order...}}.
            # We unwrap .data so callers see the order dict directly.
            def _unwrap(args):
                if not isinstance(args, list) or not args:
                    return
                env = args[0]
                if isinstance(env, dict) and "data" in env and isinstance(env["data"], dict):
                    on_order(env["data"])
                elif isinstance(env, dict):
                    on_order(env)   # already-unwrapped shape, just in case
            hub.on("GatewayUserOrder", _unwrap)
            # TopstepX fires GatewayLogout when another session evicts ours.
            # Surface it so the user knows why updates stopped.
            def _on_logout(args):
                logger.warning("TopstepX forced logout (another session took over): %s", args)
                if on_disconnect:
                    on_disconnect()
            hub.on("GatewayLogout", _on_logout)

            hub.start()
            self._hub = hub

    def stop(self) -> None:
        with self._hub_lock:
            if self._hub is not None:
                try:
                    self._hub.stop()
                except Exception as ex:
                    logger.warning("SignalR stop error: %s", ex)
                self._hub = None
        try:
            self._http.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _raise_for_status(resp: httpx.Response, label: str) -> None:
        if resp.is_success:
            return
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:200]
        raise RuntimeError(f"{label}: HTTP {resp.status_code} — {body!r}")

    @staticmethod
    def _raise_if_api_failure(body: dict, label: str) -> None:
        """ProjectX returns HTTP 200 even on business-logic failures. The real
        result is in the `success` field; we raise with the errorMessage so
        callers see the actual cause instead of silently accepting a failure."""
        if not isinstance(body, dict):
            return
        if body.get("success") is False:
            code = body.get("errorCode")
            msg = body.get("errorMessage") or f"errorCode {code}"
            # Surface in terminal too, so users can paste errors without
            # having to screenshot the status strip.
            logger.error("%s rejected: errorCode=%s message=%r body=%r",
                         label, code, msg, body)
            raise RuntimeError(f"{label}: {msg}")


def _maybe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _is_position_brackets_conflict(body: dict) -> bool:
    """TopstepX returns a specific message when Position Brackets is on and we
    try to attach our own brackets. Detect by both errorCode and message text,
    since errorCode 2 is used for other things too."""
    msg = (body.get("errorMessage") or "").lower()
    return "position brackets" in msg or "auto oco brackets" in msg
